"""Wrapper over the Telegram Bot API (aiogram 3): forum topics + operator-side long-polling.

We choose long-polling (getUpdates), NOT a webhook: the relay needs no publicly reachable inbound endpoint —
only egress to api.telegram.org (in the cloud — over IPv6, see docs/integration.md). The primitives are checked
against the official Bot API docs: createForumTopic → message_thread_id; sendMessage(..., message_thread_id=…).

aiogram is imported LAZILY in __init__/_register_handlers — the package stays importable (store/models/
contract) without aiogram installed (offline unit tests do not require it).
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Protocol

_LOG = logging.getLogger("supportal.tg")

# Operator-facing note posted into the topic when an operator's file can't be fetched (e.g. above the Bot
# API's 20 MB getFile limit). Hardcoded English like HELP_TEXT — operators read the topic in Telegram.
MEDIA_FETCH_FAILED = (
    "⚠️ Couldn't fetch that file to forward it to the user (the Telegram Bot API caps downloads at 20 MB). "
    "The user did NOT receive it — please resend a smaller file or share a link."
)


# ── Operator events (Telegram → consumer) ──────────────────────────────────────
# One structured event per operator action instead of five positional callbacks (+ a 7-tuple). Adding a field
# no longer ripples through positional signatures; a new capability is a new event + one handler method.
@dataclass(frozen=True)
class OperatorReply:
    thread_id: int
    text: str
    operator: str
    tg_message_id: int = 0


@dataclass(frozen=True)
class OperatorMediaReply:
    thread_id: int
    data: bytes
    filename: str
    mime: str
    caption: str
    operator: str
    tg_message_id: int = 0


@dataclass(frozen=True)
class OperatorCommand:
    thread_id: int
    command: str          # e.g. "/close", "/clear", "/c <key>"
    operator: str


@dataclass(frozen=True)
class OperatorEdit:
    tg_message_id: int
    new_text: str


@dataclass(frozen=True)
class OperatorDelete:
    tg_message_id: int


class OperatorHandler(Protocol):
    """What TelegramOps calls back into (one object, not five callbacks). Relay implements it."""
    async def on_message(self, ev: OperatorReply) -> None: ...
    async def on_media(self, ev: OperatorMediaReply) -> None: ...
    async def on_command(self, ev: OperatorCommand) -> None: ...
    async def on_edit(self, ev: OperatorEdit) -> None: ...
    async def on_delete(self, ev: OperatorDelete) -> None: ...

# Operator commands in the topic: /close (close), /del (recall a message — as a reply), /clear (clear both
# sides), /help (hint), /c <key> (canned reply), /ban — reserved. Telegram does NOT send bots deletion events → /del + edit-sync.
_OPERATOR_COMMANDS = ["close", "ban", "del", "clear", "help", "c"]

# Hint for the operator (printed into the topic on /help; does NOT go to the user — operator-facing).
HELP_TEXT = (
    "🛠 Operator commands in this topic:\n"
    "• Reply with text — the reply goes to the user's chat.\n"
    "• Edit your own message — the change is reflected for the user.\n"
    "• /del (as a reply to a message) — remove that message from the user.\n"
    "• /c <key> — send a canned reply (feature SUPPORT_CANNED; /c with no key lists them).\n"
    "• /clear — clear the conversation on both sides (for the user and here; the topic will be deleted).\n"
    "• /close — close the ticket.\n"
    "⚠️ Deleting a message via Telegram's own UI is NOT synced to the chat — use /del."
)


def operator_name(user) -> str:
    """Operator signature for callback/audit: @username → full name → id. Not a secret, not user data."""
    if user is None:
        return ""
    return user.username or (user.full_name or "") or str(user.id)


class TelegramOps:
    def __init__(self, bot_token: str, group_id: int, proxy: str = "") -> None:
        from aiogram import Bot, Dispatcher
        # proxy set → egress to api.telegram.org via a forward-proxy (in some networks Telegram is reachable
        # only over IPv6; an HTTP CONNECT proxy on an external host with IPv4 egress removes the blocker, see docs/integration.md §IPv6).
        if proxy:
            from aiogram.client.session.aiohttp import AiohttpSession
            self._bot = Bot(bot_token, session=AiohttpSession(proxy=proxy))
        else:
            self._bot = Bot(bot_token)
        self._dp = Dispatcher()
        self._group_id = group_id

    async def ensure_topic(self, title: str) -> int:
        """Create a forum topic in the supergroup → message_thread_id. Called once per ticket."""
        topic = await self._bot.create_forum_topic(chat_id=self._group_id, name=title[:128])
        return topic.message_thread_id

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        await self._bot.send_message(chat_id=self._group_id, message_thread_id=thread_id, text=text)

    async def delete_topic(self, thread_id: int) -> None:
        """Delete the forum topic entirely (all messages in it) — the Telegram side of /clear. Requires can_manage_topics."""
        await self._bot.delete_forum_topic(chat_id=self._group_id, message_thread_id=thread_id)

    async def react(self, tg_message_id: int, emoji: str = "👀") -> None:
        """Set a reaction on a message in the supergroup (feature SUPPORT_RECEIPTS: 👀 = the user saw the reply).
        Best-effort — the caller swallows failures (the bot may lack permission, or the message may be gone)."""
        from aiogram.types import ReactionTypeEmoji
        await self._bot.set_message_reaction(chat_id=self._group_id, message_id=tg_message_id,
                                             reaction=[ReactionTypeEmoji(emoji=emoji)])

    async def send_media(self, thread_id: int, data: bytes, filename: str, mime: str, caption: str = "") -> None:
        """Send a file into the topic: a photo for images (inline), a document otherwise."""
        from aiogram.types import BufferedInputFile
        file = BufferedInputFile(data, filename=filename or "file")
        cap = caption or None
        if mime.startswith("image/") and mime not in ("image/gif", "image/svg+xml"):
            await self._bot.send_photo(chat_id=self._group_id, message_thread_id=thread_id, photo=file, caption=cap)
        else:
            await self._bot.send_document(chat_id=self._group_id, message_thread_id=thread_id, document=file, caption=cap)

    async def _download(self, file_id: str) -> bytes:
        """Fetch file bytes from Telegram (getFile + download) — for operator-sent photos/documents."""
        file = await self._bot.get_file(file_id)
        buf = await self._bot.download_file(file.file_path)
        return buf.read()

    def _register_handlers(self, handler: OperatorHandler) -> None:
        """Supergroup topic handlers → structured events on `handler`: /del → on_delete, /close|/clear|/c|… →
        on_command, text → on_message, text EDITING → on_edit (edited_message), photo/document → on_media.

        Filters: only OUR group (chat.id == group_id) and only INSIDE a topic (message_thread_id set).
        A command is caught before text; unknown "/…" are excluded from the text handler (they do not leak
        to the user). Service messages without `.text` are filtered out by F.text. Telegram does NOT notify about
        deletion → we do a recall via an explicit `/del` command (as a reply to a message), and catch edits via edited_message.
        """
        from aiogram import F
        from aiogram.filters import Command, CommandObject
        from aiogram.types import Message

        in_our_topic = (F.chat.id == self._group_id) & F.message_thread_id.is_not(None)

        async def _handle_command(message: Message, command: CommandObject) -> None:
            if command.command == "del":
                # Message recall: the operator replies `/del` to their own message. The target is reply_to_message
                # (but not the service "topic created"). No target → ignore (never delete blindly).
                rt = message.reply_to_message
                target = rt.message_id if (rt and not rt.forum_topic_created) else 0
                if target:
                    await handler.on_delete(OperatorDelete(target))
                    _LOG.info("[op] /del thread=%s target_msg=%s", message.message_thread_id, target)
                else:
                    _LOG.info("[op] /del with no reply to a message thread=%s — ignored", message.message_thread_id)
                return
            if command.command == "help":
                # Hint for the operator — printed straight into the topic, does NOT go to the user's feed.
                await self.send_to_topic(message.message_thread_id, HELP_TEXT)
                _LOG.info("[op] /help thread=%s", message.message_thread_id)
                return
            if command.command == "c":
                # Canned reply: "/c <key>" — forward the key as the command arg (the relay expands it).
                await handler.on_command(OperatorCommand(message.message_thread_id,
                                                         f"/c {command.args or ''}".rstrip(),
                                                         operator_name(message.from_user)))
                _LOG.info("[op] /c thread=%s", message.message_thread_id)
                return
            await handler.on_command(OperatorCommand(message.message_thread_id, f"/{command.command}",
                                                     operator_name(message.from_user)))
            _LOG.info("[op] command=/%s thread=%s", command.command, message.message_thread_id)

        async def _handle_text(message: Message) -> None:
            await handler.on_message(OperatorReply(message.message_thread_id, message.text,
                                                   operator_name(message.from_user), message.message_id))
            _LOG.info("[op] reply thread=%s msg=%s len=%dchars",
                      message.message_thread_id, message.message_id, len(message.text or ""))

        async def _handle_edit(message: Message) -> None:
            await handler.on_edit(OperatorEdit(message.message_id, message.text))
            _LOG.info("[op] edit thread=%s msg=%s len=%dchars",
                      message.message_thread_id, message.message_id, len(message.text or ""))

        async def _handle_media(message: Message) -> None:
            # Operator sent a photo or a document → download the bytes and hand them to the relay.
            if message.photo:
                ps = message.photo[-1]                      # the largest available size
                file_id, filename, mime = ps.file_id, "photo.jpg", "image/jpeg"
            elif message.document:
                d = message.document
                file_id = d.file_id
                filename = d.file_name or "file"
                mime = d.mime_type or "application/octet-stream"
            else:
                return
            try:
                data = await self._download(file_id)
            except Exception:
                # A failed getFile/download (commonly a file above the Bot API's 20 MB limit) must not
                # vanish silently: log it and tell the operator in the topic that the user got nothing.
                _LOG.warning("[op] media download failed thread=%s msg=%s mime=%s — dropped (file too large?)",
                             message.message_thread_id, message.message_id, mime, exc_info=True)
                with contextlib.suppress(Exception):
                    await self.send_to_topic(message.message_thread_id, MEDIA_FETCH_FAILED)
                return
            await handler.on_media(OperatorMediaReply(message.message_thread_id, data, filename, mime,
                                                      message.caption or "", operator_name(message.from_user),
                                                      message.message_id))
            _LOG.info("[op] media thread=%s msg=%s mime=%s size=%d",
                      message.message_thread_id, message.message_id, mime, len(data))

        self._dp.message.register(_handle_command, in_our_topic, Command(commands=_OPERATOR_COMMANDS))
        self._dp.message.register(_handle_media, in_our_topic, F.photo | F.document)
        self._dp.message.register(_handle_text, in_our_topic, F.text, ~F.text.startswith("/"))
        self._dp.edited_message.register(_handle_edit, in_our_topic, F.text, ~F.text.startswith("/"))

    async def start_polling(self, handler: OperatorHandler) -> None:
        """Listen to operators via long-polling (getUpdates), dispatching structured events to `handler`.
        Blocks until the task is cancelled. Registering the edited_message handler → aiogram itself includes
        that update type in allowed_updates.

        handle_signals=False — polling runs in uvicorn's event loop (not the main "signal" context),
        uvicorn installs its own signal handlers. close_bot_session=False — the bot session is closed by app
        (lifespan → TelegramOps.close), single ownership.
        """
        self._register_handlers(handler)
        _LOG.info("[tg] supergroup long-polling group_id=%s — start", self._group_id)
        await self._dp.start_polling(self._bot, handle_signals=False, close_bot_session=False)

    async def close(self) -> None:
        await self._bot.session.close()
