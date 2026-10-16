"""Public API of the browser support widget (token-gated, polling).

These routes are the ONLY relay surface exposed to the browser (via the product's reverse-proxy,
same-origin `/support-api/v1/widget/*` → strip-prefix → here). The product ingress (`/v1/messages`) is NOT exposed outward.

Authorization: `Authorization: Bearer <token>`, where the token is minted by the PRODUCT (which knows the
logged-in user) and signed with the shared secret `SUPPORT_WIDGET_SECRET`. `user_ref` is taken FROM THE TOKEN
(server-side), not from the body — anti-spoofing. Operator replies are delivered via polling: the widget
sends the last id it has seen and receives everything newer.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import quote

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .models import IngressMessage, WidgetMessage, WidgetPoll
from .ratelimit import limiter
from .relay import Relay
from .settings import get_bool, get_int
from .tokens import verify_widget_token

_LOG = logging.getLogger("supportal.widget")

# Chars that must never reach a Content-Disposition value: quotes/backslash (break the quoted-string) and
# CR/LF/control/DEL (header injection). The filename comes from the browser (?name=) or Telegram
# (document.file_name) — untrusted.
_UNSAFE_FILENAME = re.compile(r'[\r\n"\\\x00-\x1f\x7f]')


def _content_disposition(filename: str) -> str:
    """Build a safe `Content-Disposition: attachment` header value for an untrusted filename.

    A raw `"` or CR/LF in the name would break or inject the header. We sanitize an ASCII `filename="…"`
    fallback (unsafe chars → `_`) and additionally emit an RFC 5987 `filename*=UTF-8''…` (percent-encoded,
    so newlines/quotes/non-ASCII can't break anything) that modern browsers prefer.
    """
    name = filename or "file"
    ascii_name = _UNSAFE_FILENAME.sub("_", name).encode("ascii", "ignore").decode("ascii").strip() or "file"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def _rate_limited(user_ref: str) -> bool:
    """Feature SUPPORT_RATE_LIMIT (default off): cap sends per user_ref over a window.

    Flags are read on each call so toggling them in settings.yaml applies live.
    """
    if not get_bool("SUPPORT_RATE_LIMIT", False):
        return False
    max_hits = get_int("SUPPORT_RATE_LIMIT_MAX", 20)
    window = get_int("SUPPORT_RATE_LIMIT_WINDOW", 60)
    return not limiter.allow(user_ref, max_hits, window)


class WidgetSend(BaseModel):
    text: str = Field(..., description="User text from the widget.")
    context: dict = Field(default_factory=dict,
                          description="Visitor context (url/title/custom); posted once into the topic when SUPPORT_VISITOR_CONTEXT is on.")


class WidgetCsat(BaseModel):
    score: int = Field(..., ge=1, le=5, description="Satisfaction rating 1..5 (feature SUPPORT_CSAT).")
    comment: str = Field("", description="Optional free-text comment.")


def _auth(authorization: str, secret: str) -> str:
    """user_ref from the Bearer token or 401. Fail-closed: empty secret/token/signature/expiry → reject."""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    user_ref = verify_widget_token(token, secret)
    if not user_ref:
        raise HTTPException(status_code=401, detail="bad or expired token")
    return user_ref


def build_widget_router(relay: Relay, widget_secret: str) -> APIRouter:
    router = APIRouter(prefix="/v1/widget")

    @router.post("/send")
    async def send(body: WidgetSend, authorization: str = Header("")) -> dict:
        user_ref = _auth(authorization, widget_secret)
        if _rate_limited(user_ref):
            _LOG.warning("[widget] rate-limited user=%s", user_ref[:8])
            raise HTTPException(status_code=429, detail="rate limited")
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
        # display_name/meta are NOT taken from the browser (untrusted) — name = user_ref, product is set server-side.
        # Visitor context is display-only and accepted only when the feature is on (relay caps/sanitizes it).
        ctx = body.context if get_bool("SUPPORT_VISITOR_CONTEXT", False) else {}
        res = await relay.ingress(IngressMessage(user_ref=user_ref, text=text, display_name=user_ref,
                                                 meta={"product": "supportal-widget"}, context=ctx))
        _LOG.info("[widget] send user=%s len=%dchars mid=%d", user_ref[:8], len(text), res.message_id)
        return {"ok": True, "message_id": res.message_id}

    @router.get("/messages", response_model=WidgetPoll)
    async def messages(since: int = 0, authorization: str = Header("")) -> WidgetPoll:
        user_ref = _auth(authorization, widget_secret)
        rows = relay.changes_since(user_ref, since)   # new + edited + deleted (seq cursor)
        items = [WidgetMessage(id=m.id, direction=m.direction, author=m.author, text=m.text,
                               ts=m.created_at, deleted=m.deleted, kind=m.kind, attachment_id=m.attachment_id,
                               attachment_name=m.attachment_name, attachment_mime=m.attachment_mime)
                 for m in rows]
        cursor = rows[-1].seq if rows else since
        return WidgetPoll(messages=items, cursor=cursor)

    @router.post("/upload")
    async def upload(request: Request, name: str = "", authorization: str = Header("")) -> dict:
        """Feature SUPPORT_ATTACHMENTS: raw-body file upload (no multipart). Content-Type → mime, ?name= → filename."""
        user_ref = _auth(authorization, widget_secret)
        if not get_bool("SUPPORT_ATTACHMENTS", False):
            raise HTTPException(status_code=404, detail="attachments disabled")
        if _rate_limited(user_ref):
            _LOG.warning("[widget] rate-limited (upload) user=%s", user_ref[:8])
            raise HTTPException(status_code=429, detail="rate limited")
        max_bytes = get_int("SUPPORT_ATTACHMENTS_MAX_MB", 10) * 1024 * 1024
        # Memory-DoS guard: reject BEFORE buffering when the declared size already exceeds the cap, then
        # stream with a hard cap so a missing/understated Content-Length still can't OOM the process.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise HTTPException(status_code=413, detail="file too large")
        data = bytes(buf)
        if not data:
            raise HTTPException(status_code=400, detail="empty file")
        mime = (request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
        res = await relay.add_user_attachment(user_ref, user_ref, data, name or "file", mime, "")
        _LOG.info("[widget] upload user=%s mime=%s size=%d mid=%d", user_ref[:8], mime, len(data), res.message_id)
        return {"ok": True, "message_id": res.message_id, "attachment_id": res.attachment_id}

    @router.post("/csat")
    async def csat(body: WidgetCsat, authorization: str = Header("")) -> dict:
        """Feature SUPPORT_CSAT: a satisfaction rating after the operator closed the ticket. 404 when the
        feature is off or there is no ticket to attach it to; 429 when rate-limited."""
        user_ref = _auth(authorization, widget_secret)
        if not get_bool("SUPPORT_CSAT", False):
            raise HTTPException(status_code=404, detail="csat disabled")
        if _rate_limited(user_ref):
            _LOG.warning("[widget] rate-limited (csat) user=%s", user_ref[:8])
            raise HTTPException(status_code=429, detail="rate limited")
        mid = await relay.submit_csat(user_ref, body.score, body.comment)
        if mid is None:
            raise HTTPException(status_code=404, detail="no ticket to rate")
        _LOG.info("[widget] csat user=%s score=%d mid=%d", user_ref[:8], body.score, mid)
        return {"ok": True, "message_id": mid}

    @router.get("/attachment/{attachment_id}")
    async def attachment(attachment_id: int, authorization: str = Header("")) -> Response:
        """Serve attachment bytes to the browser. Bearer-gated + ownership check (own user_ref only)."""
        user_ref = _auth(authorization, widget_secret)
        if not get_bool("SUPPORT_ATTACHMENTS", False):
            raise HTTPException(status_code=404, detail="attachments disabled")
        att = relay.get_attachment(attachment_id)
        if not att or att.user_ref != user_ref:
            raise HTTPException(status_code=404, detail="not found")
        headers = {}
        if not (att.mime or "").startswith("image/"):
            headers["Content-Disposition"] = _content_disposition(att.filename)
        return Response(content=att.data, media_type=att.mime or "application/octet-stream", headers=headers)

    return router
