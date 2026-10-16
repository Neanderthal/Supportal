"""Публичный API браузерного виджета поддержки (token-gated, polling).

Эти роуты — ЕДИНСТВЕННАЯ поверхность relay, доступная браузеру (через reverse-proxy продукта,
same-origin `/support-api/v1/widget/*` → strip-prefix → сюда). Ingress продукта (`/v1/messages`) наружу НЕ выводится.

Авторизация: `Authorization: Bearer <token>`, где токен чеканит ПРОДУКТ (знает залогиненного) и подписывает
общим секретом `SUPPORT_WIDGET_SECRET`. `user_ref` берётся ИЗ ТОКЕНА (сервер), не из тела — анти-спуфинг.
Доставка ответов оператора — polling: виджет шлёт последний виденный id, получает всё, что новее.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .models import IngressMessage, WidgetMessage, WidgetPoll
from .relay import Relay
from .tokens import verify_widget_token

_LOG = logging.getLogger("dali.support.widget")


class WidgetSend(BaseModel):
    text: str = Field(..., description="Текст пользователя из виджета.")


def _auth(authorization: str, secret: str) -> str:
    """user_ref из Bearer-токена или 401. Fail-closed: пустой секрет/токен/подпись/просрочка → отказ."""
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
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
        # display_name/meta НЕ берём из браузера (не доверяем) — имя = user_ref, продукт помечаем сервером.
        res = await relay.ingress(IngressMessage(user_ref=user_ref, text=text, display_name=user_ref,
                                                 meta={"product": "dali-widget"}))
        _LOG.info("[widget] send user=%s len=%dсимв mid=%d", user_ref[:8], len(text), res.message_id)
        return {"ok": True, "message_id": res.message_id}

    @router.get("/messages", response_model=WidgetPoll)
    async def messages(since: int = 0, authorization: str = Header("")) -> WidgetPoll:
        user_ref = _auth(authorization, widget_secret)
        rows = relay.changes_since(user_ref, since)   # новые + правленые + удалённые (seq-курсор)
        items = [WidgetMessage(id=m.id, direction=m.direction, author=m.author, text=m.text,
                               ts=m.created_at, deleted=m.deleted) for m in rows]
        cursor = rows[-1].seq if rows else since
        return WidgetPoll(messages=items, cursor=cursor)

    return router
