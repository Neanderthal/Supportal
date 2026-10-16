"""Relay's FastAPI surface: ingress from the product + health. There is NO public Telegram webhook (long-polling)."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from .models import IngressMessage, IngressResult
from .relay import Relay, sign

_LOG = logging.getLogger("supportal.api")


def build_router(relay: Relay, ingress_secret: str) -> APIRouter:
    router = APIRouter()

    @router.get("/health", tags=["health"], summary="Health probe")
    async def health() -> dict:
        return {"status": "ok", "service": "supportal"}

    @router.post("/v1/messages", response_model=IngressResult, tags=["product"],
                 summary="Product → relay: a user message",
                 description="Contour B. HMAC-signed with `SUPPORT_INGRESS_SECRET` via `X-Supportal-Signature`.")
    async def ingress(request: Request, x_supportal_signature: str = Header("")) -> IngressResult:
        body = await request.body()
        if not ingress_secret or not hmac.compare_digest(x_supportal_signature, sign(body, ingress_secret)):
            _LOG.warning("[api] ingress with a bad signature — 401")
            raise HTTPException(status_code=401, detail="bad signature")
        return await relay.ingress(IngressMessage.model_validate_json(body))

    return router
