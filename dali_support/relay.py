"""Ядро: оркестрация ingress (продукт → Telegram) и обратного пути (оператор → продукт).

Relay НЕ знает про Chainlit — только про непрозрачный `user_ref` и HTTP-callback продукта. Это держит
сервис встраиваемым в любой веб-продукт, а не только в ДаЛи.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

from .models import IngressMessage, IngressResult, OperatorEvent
from .store import Store
from .telegram_ops import TelegramOps

_LOG = logging.getLogger("dali.support.relay")


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 тела для заголовка X-Dali-Signature (обе стороны контракта сверяют compare_digest)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Relay:
    def __init__(self, store: Store, tg: TelegramOps, callback_url: str, callback_secret: str, *,
                 outbox_ttl: float = 86400.0, outbox_max_attempts: int = 12,
                 outbox_backoff_base: float = 30.0, outbox_backoff_cap: float = 1800.0) -> None:
        self._store = store
        self._tg = tg
        self._callback_url = callback_url
        self._callback_secret = callback_secret
        # Политика повторной доставки outbox (ответ оператора оффлайн-пользователю, граница A сохранена:
        # повторяем HTTP-callback, про сессии продукта ничего не знаем — только delivered из ответа).
        self._outbox_ttl = outbox_ttl
        self._outbox_max_attempts = outbox_max_attempts
        self._backoff_base = outbox_backoff_base
        self._backoff_cap = outbox_backoff_cap

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
        """Живая попытка доставки события продукту. Не доставлено (delivered=0 / сбой) → в outbox на повтор."""
        if await self._post_callback(ev):
            return
        self._store.enqueue_outbox(ev.ticket_id, ev.user_ref, ev.event, ev.text, ev.operator,
                                   attempts=1, next_attempt_at=time.time() + self._backoff(1))
        _LOG.info("[reply] не доставлено сразу (пользователь оффлайн?) — в outbox на повтор, ticket=%s",
                  ev.ticket_id)

    async def _post_callback(self, ev: OperatorEvent) -> bool:
        """POST события продукту с HMAC-подписью. True = доставлено (delivered>0 ИЛИ поля нет — простой
        продукт без отчёта о доставке). False = сбой/4xx/5xx или delivered=0 (адресат оффлайн). Fail-soft.

        Граница A: знаем только HTTP-контракт — тело события + число delivered из ответа; про сессии и
        Chainlit продукта НИЧЕГО не знаем. Отсутствие `delivered` в ответе трактуем как «доставлено»
        (обратная совместимость с продуктом, который не ведёт учёт доставки → без вечных повторов)."""
        import httpx
        body = ev.model_dump_json().encode()
        headers = {"Content-Type": "application/json", "X-Dali-Signature": sign(body, self._callback_secret)}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._callback_url, content=body, headers=headers)
        except Exception:
            _LOG.warning("[reply] callback продукту не доставлен (ticket=%s) — повторим", ev.ticket_id)
            return False
        if resp.status_code >= 400:
            _LOG.warning("[reply] callback продукта вернул %s (ticket=%s) — повторим", resp.status_code, ev.ticket_id)
            return False
        try:
            delivered = (resp.json() or {}).get("delivered")
        except Exception:
            delivered = None
        return True if delivered is None else int(delivered) > 0

    def _backoff(self, attempts: int) -> float:
        """Экспоненциальный backoff с потолком: base·2^(attempts-1), не больше cap."""
        return min(self._backoff_base * (2 ** max(0, attempts - 1)), self._backoff_cap)

    async def retry_outbox_once(self) -> int:
        """Один проход повторной доставки: разослать «дозревшие» записи outbox. Возвращает число обработанных.

        Пользователь вернулся → адаптер продукта на следующем callback запушит в его живую сессию и вернёт
        delivered>0 → помечаем delivered. Иначе откладываем по backoff; старше TTL / исчерпали попытки →
        dead (сдаёмся, WARNING). Зовётся фоновым циклом app (см. app._outbox_loop)."""
        now = time.time()
        due = self._store.outbox_due(now)
        for item in due:
            if now - item.created_at > self._outbox_ttl or item.attempts >= self._outbox_max_attempts:
                self._store.outbox_mark_dead(item.id)
                _LOG.warning("[outbox] сдаёмся: ticket=%s attempts=%d age=%.0fс — ответ оператора не доставлен",
                             item.ticket_id, item.attempts, now - item.created_at)
                continue
            ev = OperatorEvent(event=item.event, user_ref=item.user_ref, ticket_id=item.ticket_id,
                               text=item.text, operator=item.operator)
            if await self._post_callback(ev):
                self._store.outbox_mark_delivered(item.id)
                _LOG.info("[outbox] доставлено на попытке %d: ticket=%s", item.attempts + 1, item.ticket_id)
            else:
                self._store.outbox_mark_retry(item.id, item.attempts + 1,
                                              now + self._backoff(item.attempts + 1))
        return len(due)
