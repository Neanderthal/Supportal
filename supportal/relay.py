"""Core: orchestrates ingress (product → Telegram) and the reverse path (operator → product).

The relay does NOT know about any specific web chat/framework — only about the opaque `user_ref` and the
product's HTTP callback. This keeps the service embeddable into any web product.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections import defaultdict

from . import ai
from .flags import Flags
from .hours import is_within
from .models import IngressMessage, IngressResult, OperatorEvent
from .settings import get, get_bool, get_dict
from .store import Store
from .telegram_ops import (
    OperatorCommand,
    OperatorDelete,
    OperatorEdit,
    OperatorMediaReply,
    OperatorReply,
    TelegramOps,
)

_LOG = logging.getLogger("supportal.relay")

# The responder name the user SEES in the widget — always the anonymized "Operator" (the operator's real
# username/name does NOT go to the feed/browser; attribution of a specific operator stays inside the
# Telegram topic — it's their message). `operator` is still carried in OperatorEvent/logs ("via product" model).
OPERATOR_DISPLAY = "Operator"

# Author shown for AI auto-answers (feature SUPPORT_AI) — distinct from a human "Operator".
AI_DISPLAY = "Assistant"


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
        # NB: the kwarg defaults above are for DIRECT construction (tests); at runtime app.create_app always
        # passes Config.from_env values — config.py is the single source of truth for these numbers, not here.
        self._outbox_ttl = outbox_ttl
        self._outbox_max_attempts = outbox_max_attempts
        self._backoff_base = outbox_backoff_base
        self._backoff_cap = outbox_backoff_cap
        # Per-user lock serializing find-or-create of a ticket+topic. Without it, two concurrent first-contact
        # requests from one user (two tabs / a retry / send+upload) both see "no topic yet" across the
        # `await ensure_topic` and create TWO Telegram topics. One small lock per active user_ref (negligible).
        self._ticket_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Fire-and-forget background tasks (AI auto-answer): kept referenced so they aren't GC'd mid-flight.
        self._bg_tasks: set = set()

    def _spawn(self, coro) -> None:
        """Run a coroutine in the background (don't block the request). Errors are logged, not swallowed silently."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception() is None
                               or _LOG.error("[bg] background task failed: %s", t.exception(), exc_info=t.exception()))

    async def _ensure_ticket(self, user_ref: str, display_name: str) -> tuple:
        """The user's open ticket (creating it + its Telegram topic on first contact). Returns (ticket, is_new).

        Serialized per user_ref so concurrent first-contacts don't create duplicate topics: the second caller
        waits, then finds the ticket+topic the first one created (`is_new=False`)."""
        async with self._ticket_locks[user_ref]:
            ticket = self._store.get_open_by_user(user_ref) or self._store.create(user_ref, display_name)
            is_new = ticket.thread_id is None
            if is_new:
                thread_id = await self._tg.ensure_topic(display_name or user_ref)
                self._store.attach_thread(ticket.ticket_id, thread_id)
                ticket.thread_id = thread_id
            return ticket, is_new

    async def ingress(self, msg: IngressMessage) -> IngressResult:
        """A user's message → their forum topic (created on the first contact)."""
        ticket, is_new = await self._ensure_ticket(msg.user_ref, msg.display_name)
        if is_new:
            block = self._context_block(msg.context)   # one-time visitor context into the fresh topic
            if block:
                await self._tg.send_to_topic(ticket.thread_id, block)
        await self._tg.send_to_topic(ticket.thread_id, self._header(msg) + msg.text)
        self._store.touch(ticket.ticket_id)
        # Into the feed (the source of truth for widget polling). We write it AFTER a successful send to the
        # topic ON PURPOSE: the operator must receive the message, so we only record it once it's delivered to
        # Telegram. (The reverse path on_operator_message records first, because there the feed IS the delivery
        # channel.) A crash strictly between the two is a tolerated edge case: no feed row, so the widget simply
        # re-sends — preferable to a feed row for a message the operator never saw. See arch review #8.
        mid = self._store.add_message(ticket.ticket_id, ticket.user_ref, "in",
                                      msg.display_name or msg.user_ref, msg.text)
        _LOG.info("[ingress] ticket=%s thread=%s len=%dchars", ticket.ticket_id, ticket.thread_id, len(msg.text))
        if is_new:
            await self._maybe_away(ticket)   # out-of-hours auto-reply, once per ticket (on first contact)
        if get_bool(Flags.AI, False):
            # Attempt a KB-grounded auto-answer in the BACKGROUND so the send returns immediately; the answer
            # (or nothing, if the model hands off) arrives in the feed shortly after, like an operator reply.
            self._spawn(self._ai_answer(ticket, msg.text))
        return IngressResult(ticket_id=ticket.ticket_id, thread_id=ticket.thread_id, message_id=mid)

    async def _ai_answer(self, ticket, question: str) -> None:
        """Feature SUPPORT_AI: ask the LLM to answer `question` from the markdown KB; if it does (not HANDOFF),
        post the answer into the user's feed (kind=ai) + a note into the topic so the operator sees the bot replied."""
        answer = await ai.auto_answer(question)
        if not answer:
            return
        self._store.add_message(ticket.ticket_id, ticket.user_ref, "out", AI_DISPLAY, answer, kind="ai")
        if ticket.thread_id is not None:
            try:
                await self._tg.send_to_topic(ticket.thread_id, f"🤖 (auto-answer) {answer}")
            except Exception:
                _LOG.warning("[ai] failed to post the auto-answer note into topic thread=%s", ticket.thread_id,
                             exc_info=True)
        _LOG.info("[ai] auto-answer posted ticket=%s len=%dchars", ticket.ticket_id, len(answer))

    async def _maybe_away(self, ticket) -> None:
        """Feature SUPPORT_BUSINESS_HOURS: when a ticket is opened outside business hours, send a one-time
        "we're away" auto-reply into the user's feed + a note into the topic. Fail-open on a bad schedule."""
        if not get_bool(Flags.BUSINESS_HOURS, False):
            return
        if is_within(get(Flags.HOURS, ""), get(Flags.HOURS_TZ, "UTC")):
            return
        away = get(Flags.AWAY_MESSAGE,
                   "Thanks for reaching out! We're away right now and will reply during business hours.")
        if ticket.thread_id is not None:
            try:
                await self._tg.send_to_topic(ticket.thread_id, f"⏰ (auto-away) {away}")
            except Exception:
                _LOG.warning("[away] failed to post auto-away note into topic thread=%s", ticket.thread_id,
                             exc_info=True)
        self._store.add_message(ticket.ticket_id, ticket.user_ref, "out", OPERATOR_DISPLAY, away, kind="away")
        _LOG.info("[away] out-of-hours auto-reply sent ticket=%s", ticket.ticket_id)

    async def add_user_attachment(self, user_ref: str, display_name: str, data: bytes, filename: str,
                                  mime: str, caption: str = "") -> IngressResult:
        """A user's file from the widget → their forum topic (photo/document) + a feed row referencing it."""
        ticket, _is_new = await self._ensure_ticket(user_ref, display_name)
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

    def metrics(self) -> dict | None:
        """Point-in-time counts for the /metrics endpoint (feature SUPPORT_METRICS). None when the feature is off."""
        if not get_bool(Flags.METRICS, False):
            return None
        return self._store.metrics()

    def export_user(self, user_ref: str) -> dict | None:
        """GDPR data export (feature SUPPORT_GDPR): everything stored for a user_ref. None when the feature is off."""
        if not get_bool(Flags.GDPR, False):
            return None
        data = self._store.export_user(user_ref)
        _LOG.info("[gdpr] export user=%s tickets=%d messages=%d attachments=%d", user_ref[:8],
                  len(data["tickets"]), len(data["messages"]), len(data["attachments"]))
        return data

    async def delete_user(self, user_ref: str) -> dict | None:
        """GDPR erase (feature SUPPORT_GDPR): delete the user's stored data AND their Telegram topics.
        None when the feature is off. Topic deletion is best-effort (operator side); the data erase is authoritative."""
        if not get_bool(Flags.GDPR, False):
            return None
        for thread_id in self._store.thread_ids_for_user(user_ref):
            try:
                await self._tg.delete_topic(thread_id)
            except Exception:
                _LOG.warning("[gdpr] failed to delete topic thread=%s during erase (no permission?)",
                             thread_id, exc_info=True)
        counts = self._store.delete_user(user_ref)
        _LOG.info("[gdpr] erase user=%s removed=%s", user_ref[:8], counts)
        return counts

    async def mark_read(self, user_ref: str, up_to_seq: int) -> int:
        """Read receipt (feature SUPPORT_RECEIPTS): the widget reports it has seen the feed up to `up_to_seq`.
        Mark the operator's `out` messages read and, for each one newly read, set a 👀 reaction on the operator's
        Telegram message so the operator learns the user saw the reply (best-effort). Returns the count."""
        if not get_bool(Flags.RECEIPTS, False):
            return 0
        tg_ids = self._store.mark_read(user_ref, up_to_seq)
        for mid in tg_ids:
            try:
                await self._tg.react(mid, "👀")
            except Exception:
                _LOG.warning("[read] failed to set a seen reaction on tg_msg=%s", mid, exc_info=True)
        if tg_ids:
            _LOG.info("[read] user=%s marked %d operator message(s) seen", user_ref[:8], len(tg_ids))
        return len(tg_ids)

    async def submit_csat(self, user_ref: str, score: int, comment: str = "") -> int | None:
        """A post-close satisfaction rating from the widget (feature SUPPORT_CSAT).

        Records it as a feed row (so it survives reload and the widget shows it) and best-effort posts it into
        the referred ticket's topic so the operator sees the score. Returns the feed-row id, or None when the
        feature is off or there is no ticket to attach it to. Score is clamped to 1..5."""
        if not get_bool(Flags.CSAT, False):
            return None
        ticket = self._store.get_last_by_user(user_ref)
        if not ticket:
            _LOG.warning("[csat] rating for an unknown user=%s — skipping", user_ref[:8])
            return None
        score = max(1, min(5, int(score)))
        stars = "⭐" * score
        comment = (comment or "").strip()[:500]
        feed_text = f"{stars} {score}/5" + (f"\n{comment}" if comment else "")
        mid = self._store.add_message(ticket.ticket_id, ticket.user_ref, "in",
                                      ticket.display_name or ticket.user_ref, feed_text, kind="csat")
        if ticket.thread_id is not None:
            try:
                await self._tg.send_to_topic(ticket.thread_id, f"📊 CSAT: {stars} {score}/5"
                                             + (f"\n{comment}" if comment else ""))
            except Exception:
                _LOG.warning("[csat] failed to post rating into topic thread=%s (deleted?) — feed row kept",
                             ticket.thread_id, exc_info=True)
        _LOG.info("[csat] ticket=%s score=%d comment=%s", ticket.ticket_id, score, "yes" if comment else "no")
        return mid

    # ── OperatorHandler protocol (telegram_ops → relay): unpack the structured event into the domain call.
    # One object is passed to TelegramOps.start_polling instead of five positional callbacks; the on_operator_*
    # methods below hold the actual logic (and stay the direct, well-tested API).
    async def on_message(self, ev: OperatorReply) -> None:
        await self.on_operator_message(ev.thread_id, ev.text, ev.operator, ev.tg_message_id)

    async def on_media(self, ev: OperatorMediaReply) -> None:
        await self.on_operator_media(ev.thread_id, ev.data, ev.filename, ev.mime, ev.caption,
                                     ev.operator, ev.tg_message_id)

    async def on_command(self, ev: OperatorCommand) -> None:
        await self.on_operator_command(ev.thread_id, ev.command, ev.operator)

    async def on_edit(self, ev: OperatorEdit) -> None:
        await self.on_operator_edit(ev.tg_message_id, ev.new_text)

    async def on_delete(self, ev: OperatorDelete) -> None:
        await self.on_operator_delete(ev.tg_message_id)

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
        if not get_bool(Flags.ATTACHMENTS, False):
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
        """Operator commands in the topic: /close — close; /clear — clear both sides; /c <key> — canned reply."""
        ticket = self._store.get_by_thread(thread_id)
        if not ticket:
            return
        if command == "/c" or command.startswith("/c "):
            await self._send_canned(ticket, command[2:].strip(), operator)
            return
        if command == "/close":
            self._store.close(ticket.ticket_id)
            notice = get(Flags.CLOSED_NOTICE, "This ticket was closed by the operator.")
            # Feature SUPPORT_CSAT: tag the close notice so the widget renders a rating prompt below it.
            kind = "csat_request" if get_bool(Flags.CSAT, False) else "msg"
            self._store.add_message(ticket.ticket_id, ticket.user_ref, "out", OPERATOR_DISPLAY, notice, kind=kind)
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

    async def _send_canned(self, ticket, key: str, operator: str) -> None:
        """Feature SUPPORT_CANNED: expand "/c <key>" to a preset reply and send it to the user (feed `out`
        row + the product callback), with a confirmation note into the topic. Unknown/empty key → list the
        available keys into the topic (operator-facing, nothing goes to the user). Off → operator-facing hint."""
        if not get_bool(Flags.CANNED, False):
            if ticket.thread_id is not None:
                await self._tg.send_to_topic(ticket.thread_id, "🗂 Canned replies are disabled (SUPPORT_CANNED).")
            return
        replies = get_dict(Flags.CANNED_REPLIES, {})
        if not key or key not in replies:
            available = ", ".join(sorted(replies)) or "(none configured)"
            if ticket.thread_id is not None:
                await self._tg.send_to_topic(ticket.thread_id, f"🗂 Canned replies: {available}")
            _LOG.info("[canned] ticket=%s unknown/empty key=%r — listed %d keys", ticket.ticket_id, key, len(replies))
            return
        text = replies[key]
        self._store.add_message(ticket.ticket_id, ticket.user_ref, "out", OPERATOR_DISPLAY, text)
        if ticket.thread_id is not None:
            await self._tg.send_to_topic(ticket.thread_id, f"↩️ (canned: {key}) {text}")
        await self._dispatch(OperatorEvent(event="message", user_ref=ticket.user_ref,
                                           ticket_id=ticket.ticket_id, text=text, operator=operator))
        self._store.touch(ticket.ticket_id)
        _LOG.info("[canned] ticket=%s key=%s len=%dchars", ticket.ticket_id, key, len(text))

    def _header(self, msg: IngressMessage) -> str:
        """Message header in the topic: who is writing + context (product/page). Not a secret."""
        bits = [f"👤 {msg.display_name or msg.user_ref}"]
        bits += [f"{k}={msg.meta[k]}" for k in ("product", "page") if msg.meta.get(k)]
        return " · ".join(bits) + "\n"

    def _context_block(self, context: dict) -> str | None:
        """One-time, operator-facing visitor-context block for a new topic. Gated by SUPPORT_VISITOR_CONTEXT.

        Display-only: capped field count/length and newlines stripped (it goes into a plain Telegram message).
        Returns None when the feature is off or there's nothing to show.
        """
        if not get_bool(Flags.VISITOR_CONTEXT, False) or not isinstance(context, dict) or not context:
            return None
        lines = ["🧭 Visitor context"]
        for key, value in list(context.items())[:12]:
            if value is None:
                continue
            k = str(key)[:40].replace("\n", " ").strip()
            v = str(value)[:300].replace("\n", " ").strip()
            if k and v:
                lines.append(f"{k}: {v}")
        return "\n".join(lines) if len(lines) > 1 else None

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
