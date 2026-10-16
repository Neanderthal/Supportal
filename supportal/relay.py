"""Core: orchestrates ingress (product → Telegram) and the reverse path (operator → product).

The relay does NOT know about any specific web chat/framework — only about the opaque `user_ref` and the
product's HTTP callback. This keeps the service embeddable into any web product.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

from .models import IngressMessage, IngressResult, OperatorEvent
from .settings import get, get_bool
from .store import Store
from .telegram_ops import TelegramOps

_LOG = logging.getLogger("supportal.relay")

# The responder name the user SEES in the widget — always the anonymized "Operator" (the operator's real
# username/name does NOT go to the feed/browser; attribution of a specific operator stays inside the
# Telegram topic — it's their message). `operator` is still carried in OperatorEvent/logs ("via product" model).
OPERATOR_DISPLAY = "Operator"


def sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 of the body for the X-Supportal-Signature header (both sides of the contract verify with compare_digest)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class Relay:
    def __init__(self, store: Store, tg: TelegramOps, callback_url: str, callback_secret: str, *,
                 outbox_ttl: float = 86400.0, outbox_max_attempts: int = 12,
                 outbox_backoff_base: float = 30.0, outbox_backoff_cap: float = 1800.0) -> None:
        self._store = store
        self._tg = tg
        self._callback_url = callback_url
        self._callback_secret = callback_secret
        # Outbox retry policy (operator reply to an offline user, boundary A preserved:
        # we retry the HTTP callback and know nothing about the product's sessions — only delivered from the response).
        self._outbox_ttl = outbox_ttl
        self._outbox_max_attempts = outbox_max_attempts
        self._backoff_base = outbox_backoff_base
        self._backoff_cap = outbox_backoff_cap

    async def ingress(self, msg: IngressMessage) -> IngressResult:
        """A user's message → their forum topic (created on the first contact)."""
        ticket = self._store.get_open_by_user(msg.user_ref) or self._store.create(msg.user_ref, msg.display_name)
        if ticket.thread_id is None:
            thread_id = await self._tg.ensure_topic(msg.display_name or msg.user_ref)
            self._store.attach_thread(ticket.ticket_id, thread_id)
            ticket.thread_id = thread_id
        await self._tg.send_to_topic(ticket.thread_id, self._header(msg) + msg.text)
        self._store.touch(ticket.ticket_id)
        # Into the feed (the source of truth for widget polling). We write it AFTER a successful send to the topic.
        mid = self._store.add_message(ticket.ticket_id, ticket.user_ref, "in",
                                      msg.display_name or msg.user_ref, msg.text)
        _LOG.info("[ingress] ticket=%s thread=%s len=%dchars", ticket.ticket_id, ticket.thread_id, len(msg.text))
        return IngressResult(ticket_id=ticket.ticket_id, thread_id=ticket.thread_id, message_id=mid)

    async def add_user_attachment(self, user_ref: str, display_name: str, data: bytes, filename: str,
                                  mime: str, caption: str = "") -> IngressResult:
        """A user's file from the widget → their forum topic (photo/document) + a feed row referencing it."""
        ticket = self._store.get_open_by_user(user_ref) or self._store.create(user_ref, display_name)
        if ticket.thread_id is None:
            thread_id = await self._tg.ensure_topic(display_name or user_ref)
            self._store.attach_thread(ticket.ticket_id, thread_id)
            ticket.thread_id = thread_id
        await self._tg.send_media(ticket.thread_id, data, filename, mime, caption)
        self._store.touch(ticket.ticket_id)
        att_id = self._store.add_attachment(user_ref, "in", filename, mime, data)
        mid = self._store.add_message(ticket.ticket_id, user_ref, "in", display_name or user_ref, caption,
                                      attachment_id=att_id)
        _LOG.info("[ingress] attachment ticket=%s thread=%s mime=%s size=%d",
                  ticket.ticket_id, ticket.thread_id, mime, len(data))
        return IngressResult(ticket_id=ticket.ticket_id, thread_id=ticket.thread_id,
                             message_id=mid, attachment_id=att_id)

    def changes_since(self, user_ref: str, after_seq: int):
        """Changes to the user's feed with seq > after_seq (new/edited/deleted) — widget polling."""
        return self._store.changes_since(user_ref, after_seq)

    def get_attachment(self, attachment_id: int):
        """Fetch a stored attachment (bytes + metadata) for serving to the browser."""
        return self._store.get_attachment(attachment_id)

    async def on_operator_message(self, thread_id: int, text: str, operator: str,
                                  tg_message_id: int = 0) -> None:
        """An operator reply from the topic → into the feed (the widget picks it up via polling) + an optional callback to the product.

        tg_message_id — the Telegram message id: needed so a later edit/`/del` of the same message
        finds this feed row (see on_operator_edit/on_operator_delete)."""
        ticket = self._store.get_by_thread(thread_id)
        if not ticket:
            _LOG.warning("[reply] reply in an unknown topic thread=%s — skipping", thread_id)
            return
        self._store.add_message(ticket.ticket_id, ticket.user_ref, "out", OPERATOR_DISPLAY, text,
                                tg_message_id=tg_message_id)
        await self._dispatch(OperatorEvent(event="message", user_ref=ticket.user_ref,
                                           ticket_id=ticket.ticket_id, text=text, operator=operator))
        self._store.touch(ticket.ticket_id)

    async def on_operator_media(self, thread_id: int, data: bytes, filename: str, mime: str,
                                caption: str, operator: str, tg_message_id: int = 0) -> None:
        """An operator photo/document from the topic → store the file + a feed row. Gated by SUPPORT_ATTACHMENTS."""
        if not get_bool("SUPPORT_ATTACHMENTS", False):
            _LOG.info("[media] attachments disabled — ignoring operator media thread=%s", thread_id)
            return
        ticket = self._store.get_by_thread(thread_id)
        if not ticket:
            _LOG.warning("[media] media in an unknown topic thread=%s — skipping", thread_id)
            return
        att_id = self._store.add_attachment(ticket.user_ref, "out", filename, mime, data)
        self._store.add_message(ticket.ticket_id, ticket.user_ref, "out", OPERATOR_DISPLAY, caption,
                                tg_message_id=tg_message_id, attachment_id=att_id)
        await self._dispatch(OperatorEvent(event="message", user_ref=ticket.user_ref, ticket_id=ticket.ticket_id,
                                           text=caption or f"📎 {filename}", operator=operator))
        self._store.touch(ticket.ticket_id)
        _LOG.info("[media] operator attachment ticket=%s mime=%s size=%d", ticket.ticket_id, mime, len(data))

    async def on_operator_edit(self, tg_message_id: int, new_text: str) -> None:
        """The operator edited their message in Telegram (edited_message) → update the feed row."""
        row = self._store.update_message_by_tg(tg_message_id, new_text)
        if row:
            _LOG.info("[edit] tg_msg=%s updated (ticket=%s)", tg_message_id, row.ticket_id)

    async def on_operator_delete(self, tg_message_id: int) -> None:
        """The operator recalled a message (`/del` as a reply) → soft-delete the feed row (the widget removes the bubble)."""
        row = self._store.delete_message_by_tg(tg_message_id)
        if row:
            _LOG.info("[del] tg_msg=%s recalled (ticket=%s)", tg_message_id, row.ticket_id)

    async def on_operator_command(self, thread_id: int, command: str, operator: str) -> None:
        """Operator commands in the topic: /close — close the ticket; /clear — clear both sides (feed + topic)."""
        ticket = self._store.get_by_thread(thread_id)
        if not ticket:
            return
        if command == "/close":
            self._store.close(ticket.ticket_id)
            notice = get("SUPPORT_CLOSED_NOTICE", "This ticket was closed by the operator.")
            self._store.add_message(ticket.ticket_id, ticket.user_ref, "out", OPERATOR_DISPLAY, notice)
            await self._dispatch(OperatorEvent(event="ticket_closed", user_ref=ticket.user_ref,
                                               ticket_id=ticket.ticket_id, operator=operator))
        elif command == "/clear":
            # Clear both sides: the widget feed (all rows → deleted, the widget removes the bubbles) + delete
            # the Telegram topic + reset the ticket (the user's next message creates a fresh topic).
            n = self._store.clear_messages(ticket.user_ref)
            self._store.close(ticket.ticket_id)
            try:
                await self._tg.delete_topic(thread_id)
            except Exception:
                _LOG.warning("[clear] failed to delete topic thread=%s (no permission?) — the feed is cleared anyway",
                             thread_id, exc_info=True)
            _LOG.info("[clear] ticket=%s cleared (%d feed rows, topic deleted, ticket reset)",
                      ticket.ticket_id, n)

    def _header(self, msg: IngressMessage) -> str:
        """Message header in the topic: who is writing + context (product/page). Not a secret."""
        bits = [f"👤 {msg.display_name or msg.user_ref}"]
        bits += [f"{k}={msg.meta[k]}" for k in ("product", "page") if msg.meta.get(k)]
        return " · ".join(bits) + "\n"

    async def _dispatch(self, ev: OperatorEvent) -> None:
        """A live attempt to deliver the event to the product. Not delivered (delivered=0 / failure) → into the outbox for retry.

        "Direct widget" model (callback_url not set): we do NOT send to the product — the event is already in the
        messages feed, the browser widget picks it up via polling (`/v1/widget/messages`). callback/outbox remain for
        the "via product" model (`SUPPORT_PRODUCT_CALLBACK_URL` set) and are not broken."""
        if not self._callback_url:
            return
        if await self._post_callback(ev):
            return
        self._store.enqueue_outbox(ev.ticket_id, ev.user_ref, ev.event, ev.text, ev.operator,
                                   attempts=1, next_attempt_at=time.time() + self._backoff(1))
        _LOG.info("[reply] not delivered immediately (user offline?) — into the outbox for retry, ticket=%s",
                  ev.ticket_id)

    async def _post_callback(self, ev: OperatorEvent) -> bool:
        """POST the event to the product with an HMAC signature. True = delivered (delivered>0 OR the field is absent — a simple
        product with no delivery report). False = failure/4xx/5xx or delivered=0 (recipient offline). Fail-soft.

        Boundary A: we know only the HTTP contract — the event body + the delivered count from the response; we know
        NOTHING about the product's sessions. An absent `delivered` in the response is treated as "delivered"
        (backward compatibility with a product that does not track delivery → no endless retries)."""
        import httpx
        body = ev.model_dump_json().encode()
        headers = {"Content-Type": "application/json", "X-Supportal-Signature": sign(body, self._callback_secret)}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._callback_url, content=body, headers=headers)
        except Exception:
            _LOG.warning("[reply] callback to the product not delivered (ticket=%s) — will retry", ev.ticket_id)
            return False
        if resp.status_code >= 400:
            _LOG.warning("[reply] product callback returned %s (ticket=%s) — will retry", resp.status_code, ev.ticket_id)
            return False
        try:
            delivered = (resp.json() or {}).get("delivered")
        except Exception:
            delivered = None
        return True if delivered is None else int(delivered) > 0

    def _backoff(self, attempts: int) -> float:
        """Exponential backoff with a ceiling: base·2^(attempts-1), no more than cap."""
        return min(self._backoff_base * (2 ** max(0, attempts - 1)), self._backoff_cap)

    async def retry_outbox_once(self) -> int:
        """One retry-delivery pass: send out the "due" outbox records. Returns the number processed.

        The user came back → the product adapter, on the next callback, pushes into their live session and returns
        delivered>0 → we mark it delivered. Otherwise we postpone by backoff; older than TTL / attempts exhausted →
        dead (give up, WARNING). Called by app's background loop (see app._outbox_loop)."""
        now = time.time()
        due = self._store.outbox_due(now)
        for item in due:
            if now - item.created_at > self._outbox_ttl or item.attempts >= self._outbox_max_attempts:
                self._store.outbox_mark_dead(item.id)
                _LOG.warning("[outbox] giving up: ticket=%s attempts=%d age=%.0fs — operator reply not delivered",
                             item.ticket_id, item.attempts, now - item.created_at)
                continue
            ev = OperatorEvent(event=item.event, user_ref=item.user_ref, ticket_id=item.ticket_id,
                               text=item.text, operator=item.operator)
            if await self._post_callback(ev):
                self._store.outbox_mark_delivered(item.id)
                _LOG.info("[outbox] delivered on attempt %d: ticket=%s", item.attempts + 1, item.ticket_id)
            else:
                self._store.outbox_mark_retry(item.id, item.attempts + 1,
                                              now + self._backoff(item.attempts + 1))
        return len(due)
