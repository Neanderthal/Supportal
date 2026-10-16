"""Relay's FastAPI surface: ingress from the product + health. There is NO public Telegram webhook (long-polling)."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response

from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from .metrics import render as render_metrics
from .models import IngressMessage, IngressResult
from .relay import Relay, sign

_LOG = logging.getLogger("supportal.api")


def build_router(relay: Relay, ingress_secret: str) -> APIRouter:
    router = APIRouter()

    async def _verified_body(request: Request, signature: str) -> bytes:
        """Return the raw body iff the HMAC signature checks out, else 401 (shared by the HMAC-signed routes)."""
        body = await request.body()
        if not ingress_secret or not hmac.compare_digest(signature, sign(body, ingress_secret)):
            _LOG.warning("[api] request with a bad signature — 401")
            raise HTTPException(status_code=401, detail="bad signature")
        return body

    @router.get("/health", tags=["health"], summary="Health probe")
    async def health() -> dict:
        return {"status": "ok", "service": "supportal"}

    @router.get("/metrics", tags=["health"], summary="Prometheus metrics (feature SUPPORT_METRICS)",
                description="Prometheus text exposition. Off by default; expose only on an internal network.")
    async def metrics() -> Response:
        counts = relay.metrics()
        if counts is None:
            raise HTTPException(status_code=404, detail="metrics disabled")
        return Response(content=render_metrics(counts), media_type=METRICS_CONTENT_TYPE)

    @router.post("/v1/messages", response_model=IngressResult, tags=["product"],
                 summary="Product → relay: a user message",
                 description="Contour B. HMAC-signed with `SUPPORT_INGRESS_SECRET` via `X-Supportal-Signature`.")
    async def ingress(request: Request, x_supportal_signature: str = Header("")) -> IngressResult:
        body = await _verified_body(request, x_supportal_signature)
        return await relay.ingress(IngressMessage.model_validate_json(body))

    @router.post("/v1/admin/export", tags=["product"], summary="GDPR: export a user's data (feature SUPPORT_GDPR)",
                 description="Contour B (HMAC). Body `{user_ref}`. Returns the user's tickets/messages/attachments.")
    async def gdpr_export(request: Request, x_supportal_signature: str = Header("")) -> dict:
        body = await _verified_body(request, x_supportal_signature)
        user_ref = _user_ref(body)
        data = relay.export_user(user_ref)
        if data is None:
            raise HTTPException(status_code=404, detail="gdpr disabled")
        return data

    @router.post("/v1/admin/delete", tags=["product"], summary="GDPR: erase a user's data (feature SUPPORT_GDPR)",
                 description="Contour B (HMAC). Body `{user_ref}`. Hard-deletes the user's data + Telegram topics.")
    async def gdpr_delete(request: Request, x_supportal_signature: str = Header("")) -> dict:
        body = await _verified_body(request, x_supportal_signature)
        user_ref = _user_ref(body)
        counts = await relay.delete_user(user_ref)
        if counts is None:
            raise HTTPException(status_code=404, detail="gdpr disabled")
        return {"ok": True, "deleted": counts}

    return router


def _user_ref(body: bytes) -> str:
    """Parse {"user_ref": "..."} from a verified body; 400 on a missing/empty value."""
    import json
    try:
        ref = (json.loads(body or b"{}") or {}).get("user_ref", "")
    except ValueError:
        ref = ""
    ref = (ref or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="user_ref required")
    return ref
