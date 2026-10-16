"""Ядро: оркестрация ingress (продукт → Telegram) и обратного пути (оператор → продукт).

Relay НЕ знает про конкретный веб-чат/фреймворк — только про непрозрачный `user_ref` и HTTP-callback
продукта. Это держит сервис встраиваемым в любой веб-продукт.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

from .models import IngressMessage, IngressResult, OperatorEvent
from .store import Store
from .telegram_ops import TelegramOps

_LOG = logging.getLogger("supportal.relay")

# Имя отвечающего, которое ВИДИТ пользователь в виджете — всегда обезличенное «Оператор» (реальный
# username/имя оператора НЕ уходит в ленту/браузер; атрибуция конкретного оператора остаётся в самом
# Telegram-топике — это его сообщение). `operator` всё ещё несётся в OperatorEvent/логи (модель «через продукт»).
OPERATOR_DISPLAY = "Оператор"


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 тела для заголовка X-Supportal-Signature (обе стороны контракта сверяют compare_digest)."""
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
        # В ленту (источник правды для polling виджета). Пишем ПОСЛЕ успешной отправки в топик.
        mid = self._store.add_message(ticket.ticket_id, ticket.user_ref, "in",
                                      msg.display_name or msg.user_ref, msg.text)
        _LOG.info("[ingress] ticket=%s thread=%s len=%dсимв", ticket.ticket_id, ticket.thread_id, len(msg.text))
        return IngressResult(ticket_id=ticket.ticket_id, thread_id=ticket.thread_id, message_id=mid)

    def changes_since(self, user_ref: str, after_seq: int):
        """Изменения ленты пользователя с seq > after_seq (новые/правленые/удалённые) — polling виджета."""
        return self._store.changes_since(user_ref, after_seq)

    async def on_operator_message(self, thread_id: int, text: str, operator: str,
                                  tg_message_id: int = 0) -> None:
        """Ответ оператора из топика → в ленту (виджет заберёт polling-ом) + опц. callback продукту.

        tg_message_id — id сообщения в Telegram: нужен, чтобы последующий edit/`/del` того же сообщения
        нашёл эту строку ленты (см. on_operator_edit/on_operator_delete)."""
        ticket = self._store.get_by_thread(thread_id)
        if not ticket:
            _LOG.warning("[reply] ответ в неизвестном топике thread=%s — пропускаю", thread_id)
            return
        self._store.add_message(ticket.ticket_id, ticket.user_ref, "out", OPERATOR_DISPLAY, text,
                                tg_message_id=tg_message_id)
        await self._dispatch(OperatorEvent(event="message", user_ref=ticket.user_ref,
                                           ticket_id=ticket.ticket_id, text=text, operator=operator))
        self._store.touch(ticket.ticket_id)

    async def on_operator_edit(self, tg_message_id: int, new_text: str) -> None:
        """Оператор отредактировал своё сообщение в Telegram (edited_message) → обновить строку ленты."""
        row = self._store.update_message_by_tg(tg_message_id, new_text)
        if row:
            _LOG.info("[edit] tg_msg=%s обновлено (ticket=%s)", tg_message_id, row.ticket_id)

    async def on_operator_delete(self, tg_message_id: int) -> None:
        """Оператор отозвал сообщение (`/del` ответом) → мягко удалить строку ленты (виджет уберёт пузырь)."""
        row = self._store.delete_message_by_tg(tg_message_id)
        if row:
            _LOG.info("[del] tg_msg=%s отозвано (ticket=%s)", tg_message_id, row.ticket_id)

    async def on_operator_command(self, thread_id: int, command: str, operator: str) -> None:
        """Команды оператора в топике: /close — закрыть тикет; /clear — очистить обе стороны (лента + топик)."""
        ticket = self._store.get_by_thread(thread_id)
        if not ticket:
            return
        if command == "/close":
            self._store.close(ticket.ticket_id)
            self._store.add_message(ticket.ticket_id, ticket.user_ref, "out",
                                    OPERATOR_DISPLAY, "Обращение закрыто оператором.")
            await self._dispatch(OperatorEvent(event="ticket_closed", user_ref=ticket.user_ref,
                                               ticket_id=ticket.ticket_id, operator=operator))
        elif command == "/clear":
            # Очистить обе стороны: лента виджета (все строки → deleted, виджет уберёт пузыри) + удалить
            # топик в Telegram + сбросить тикет (следующее сообщение пользователя создаст свежий топик).
            n = self._store.clear_messages(ticket.user_ref)
            self._store.close(ticket.ticket_id)
            try:
                await self._tg.delete_topic(thread_id)
            except Exception:
                _LOG.warning("[clear] не удалось удалить топик thread=%s (нет прав?) — лента всё равно очищена",
                             thread_id, exc_info=True)
            _LOG.info("[clear] ticket=%s очищено (%d строк ленты, топик удалён, тикет сброшен)",
                      ticket.ticket_id, n)

    def _header(self, msg: IngressMessage) -> str:
        """Шапка сообщения в топике: кто пишет + контекст (продукт/страница). Не секрет."""
        bits = [f"👤 {msg.display_name or msg.user_ref}"]
        bits += [f"{k}={msg.meta[k]}" for k in ("product", "page") if msg.meta.get(k)]
        return " · ".join(bits) + "\n"

    async def _dispatch(self, ev: OperatorEvent) -> None:
        """Живая попытка доставки события продукту. Не доставлено (delivered=0 / сбой) → в outbox на повтор.

        Модель «прямой виджет» (callback_url не задан): продукту НЕ шлём — событие уже в ленте messages,
        браузерный виджет заберёт его polling-ом (`/v1/widget/messages`). callback/outbox остаются для
        модели «через продукт» (задан `SUPPORT_PRODUCT_CALLBACK_URL`) и не рвутся."""
        if not self._callback_url:
            return
        if await self._post_callback(ev):
            return
        self._store.enqueue_outbox(ev.ticket_id, ev.user_ref, ev.event, ev.text, ev.operator,
                                   attempts=1, next_attempt_at=time.time() + self._backoff(1))
        _LOG.info("[reply] не доставлено сразу (пользователь оффлайн?) — в outbox на повтор, ticket=%s",
                  ev.ticket_id)

    async def _post_callback(self, ev: OperatorEvent) -> bool:
        """POST события продукту с HMAC-подписью. True = доставлено (delivered>0 ИЛИ поля нет — простой
        продукт без отчёта о доставке). False = сбой/4xx/5xx или delivered=0 (адресат оффлайн). Fail-soft.

        Граница A: знаем только HTTP-контракт — тело события + число delivered из ответа; про сессии
        продукта НИЧЕГО не знаем. Отсутствие `delivered` в ответе трактуем как «доставлено»
        (обратная совместимость с продуктом, который не ведёт учёт доставки → без вечных повторов)."""
        import httpx
        body = ev.model_dump_json().encode()
        headers = {"Content-Type": "application/json", "X-Supportal-Signature": sign(body, self._callback_secret)}
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
