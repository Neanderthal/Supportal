"""FastAPI-поверхность relay: ingress от продукта + health. Публичного Telegram-webhook НЕТ (long-polling)."""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from .models import IngressMessage, IngressResult
from .relay import Relay, sign

_LOG = logging.getLogger("supportal.api")


def build_router(relay: Relay, ingress_secret: str) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "supportal"}

    @router.post("/v1/messages", response_model=IngressResult)
    async def ingress(request: Request, x_supportal_signature: str = Header("")) -> IngressResult:
        body = await request.body()
        if not ingress_secret or not hmac.compare_digest(x_supportal_signature, sign(body, ingress_secret)):
            _LOG.warning("[api] ingress с неверной подписью — 401")
            raise HTTPException(status_code=401, detail="bad signature")
        return await relay.ingress(IngressMessage.model_validate_json(body))

    return router
