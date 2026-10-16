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

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .models import IngressMessage, WidgetMessage, WidgetPoll
from .ratelimit import limiter
from .relay import Relay
from .settings import get_bool, get_int
from .tokens import verify_widget_token

_LOG = logging.getLogger("supportal.widget")


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
        res = await relay.ingress(IngressMessage(user_ref=user_ref, text=text, display_name=user_ref,
                                                 meta={"product": "supportal-widget"}))
        _LOG.info("[widget] send user=%s len=%dchars mid=%d", user_ref[:8], len(text), res.message_id)
        return {"ok": True, "message_id": res.message_id}

    @router.get("/messages", response_model=WidgetPoll)
    async def messages(since: int = 0, authorization: str = Header("")) -> WidgetPoll:
        user_ref = _auth(authorization, widget_secret)
        rows = relay.changes_since(user_ref, since)   # new + edited + deleted (seq cursor)
        items = [WidgetMessage(id=m.id, direction=m.direction, author=m.author, text=m.text,
                               ts=m.created_at, deleted=m.deleted) for m in rows]
        cursor = rows[-1].seq if rows else since
        return WidgetPoll(messages=items, cursor=cursor)

    return router
