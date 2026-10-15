"""DTO HTTP-контракта dali-support. Это и есть граница встраивания — продукт знает только эти формы."""
from __future__ import annotations

from pydantic import BaseModel, Field


class IngressMessage(BaseModel):
    """Продукт → relay: сообщение пользователя в поддержку."""

    user_ref: str = Field(..., description="Стабильный непрозрачный id пользователя (у ДаЛи — нормализованный логин). Ключ тикета.")
    text: str = Field(..., description="Текст пользователя.")
    display_name: str = Field("", description="Человекочитаемое имя — в заголовок топика/шапку (не секрет).")
    meta: dict = Field(default_factory=dict, description="Доп. контекст оператору (продукт, страница) — показывается, не парсится.")


class IngressResult(BaseModel):
    """Relay → продукт: подтверждение приёма ingress."""

    ok: bool = True
    ticket_id: str
    thread_id: int


class OperatorEvent(BaseModel):
    """Relay → продукт (callback): ответ оператора или системное событие тикета."""

    event: str = Field("message", description="message | ticket_closed")
    user_ref: str
    ticket_id: str
    text: str = ""
    operator: str = Field("", description="Кто ответил (для показа/аудита).")
