"""Ядро: оркестрация ingress (продукт → Telegram) и обратного пути (оператор → продукт).

Relay НЕ знает про Chainlit — только про непрозрачный `user_ref` и HTTP-callback продукта. Это держит
сервис встраиваемым в любой веб-продукт, а не только в ДаЛи.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from .models import IngressMessage, IngressResult, OperatorEvent
from .store import Store
from .telegram_ops import TelegramOps

_LOG = logging.getLogger("dali.support.relay")


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 тела для заголовка X-Dali-Signature (обе стороны контракта сверяют compare_digest)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Relay:
    def __init__(self, store: Store, tg: TelegramOps, callback_url: str, callback_secret: str) -> None:
        self._store = store
        self._tg = tg
        self._callback_url = callback_url
        self._callback_secret = callback_secret

    async def ingress(self, msg: IngressMessage) -> IngressResult:
        """Сообщение пользователя → его forum-топик (создаётся при первом обращении)."""
        ticket = self._store.get_open_by_user(msg.user_ref) or self._store.create(msg.user_ref, msg.display_name)
        if ticket.thread_id is None:
            thread_id = await self._tg.ensure_topic(msg.display_name or msg.user_ref)
            self._store.attach_thread(ticket.ticket_id, thread_id)
            ticket.thread_id = thread_id
        await self._tg.send_to_topic(ticket.thread_id, self._header(msg) + msg.text)
        self._store.touch(ticket.ticket_id)
        _LOG.info("[ingress] ticket=%s thread=%s len=%dсимв", ticket.ticket_id, ticket.thread_id, len(msg.text))
        return IngressResult(ticket_id=ticket.ticket_id, thread_id=ticket.thread_id)

    async def on_operator_message(self, thread_id: int, text: str, operator: str) -> None:
        """Ответ оператора из топика → callback продукту (тот доставит в живую сессию)."""
        ticket = self._store.get_by_thread(thread_id)
        if not ticket:
            _LOG.warning("[reply] ответ в неизвестном топике thread=%s — пропускаю", thread_id)
            return
        await self._dispatch(OperatorEvent(event="message", user_ref=ticket.user_ref,
                                           ticket_id=ticket.ticket_id, text=text, operator=operator))
        self._store.touch(ticket.ticket_id)

    async def on_operator_command(self, thread_id: int, command: str, operator: str) -> None:
        """Команды оператора в топике. v1: /close — закрыть тикет (топик можно архивировать отдельно)."""
        ticket = self._store.get_by_thread(thread_id)
        if not ticket:
            return
        if command == "/close":
            self._store.close(ticket.ticket_id)
            await self._dispatch(OperatorEvent(event="ticket_closed", user_ref=ticket.user_ref,
                                               ticket_id=ticket.ticket_id, operator=operator))

    def _header(self, msg: IngressMessage) -> str:
        """Шапка сообщения в топике: кто пишет + контекст (продукт/страница). Не секрет."""
        bits = [f"👤 {msg.display_name or msg.user_ref}"]
        bits += [f"{k}={msg.meta[k]}" for k in ("product", "page") if msg.meta.get(k)]
        return " · ".join(bits) + "\n"

    async def _dispatch(self, ev: OperatorEvent) -> None:
        """POST события продукту с HMAC-подписью X-Dali-Signature. Fail-soft (лог, не падаем)."""
        import httpx
        body = ev.model_dump_json().encode()
        headers = {"Content-Type": "application/json", "X-Dali-Signature": sign(body, self._callback_secret)}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._callback_url, content=body, headers=headers)
            if resp.status_code >= 400:
                _LOG.warning("[reply] callback продукта вернул %s (ticket=%s)", resp.status_code, ev.ticket_id)
        except Exception:
            _LOG.exception("[reply] callback продукту не доставлен (ticket=%s) — ответ оператора потерян", ev.ticket_id)
