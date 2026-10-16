"""Ph2 — Telegram side offline: operator_name + topic handler registration/routing.

aiogram is a main dep (installed), but the network is not needed: Bot("123456:abc") is constructed locally
(validate_token checks only the FORMAT of the token), start_polling is mocked. We check: operator_name;
start_polling registers 2 handlers and calls polling with handle_signals=False/close_bot_session=False;
/close→on_command and plain text→on_message are routed with the correct (thread_id, …, operator).
The real end-to-end is a manual smoke test on a test supergroup (plan Ph2).
"""
import asyncio
import io
from types import SimpleNamespace

from supportal.telegram_ops import (
    HELP_TEXT,
    MEDIA_FETCH_FAILED,
    OperatorCommand,
    OperatorDelete,
    OperatorEdit,
    OperatorMediaReply,
    OperatorReply,
    TelegramOps,
    operator_name,
)

_DUMMY_TOKEN = "123456:abc"      # valid by format; the network is not touched until a bot method is called
_GROUP_ID = -1001234567890


class _RecordingHandler:
    """Captures the structured OperatorHandler events (the single object start_polling now takes)."""
    def __init__(self) -> None:
        self.messages: list = []
        self.media: list = []
        self.commands: list = []
        self.edits: list = []
        self.deletes: list = []

    async def on_message(self, ev): self.messages.append(ev)
    async def on_media(self, ev): self.media.append(ev)
    async def on_command(self, ev): self.commands.append(ev)
    async def on_edit(self, ev): self.edits.append(ev)
    async def on_delete(self, ev): self.deletes.append(ev)


class _FakeBot:
    """A stand-in for the aiogram Bot: async methods that record calls instead of touching the network."""
    def __init__(self) -> None:
        self.calls: list = []
        self.session = SimpleNamespace(close=self._close_session)

    async def create_forum_topic(self, chat_id, name):
        self.calls.append(("create_forum_topic", chat_id, name)); return SimpleNamespace(message_thread_id=4242)

    async def send_message(self, chat_id, message_thread_id, text):
        self.calls.append(("send_message", chat_id, message_thread_id, text))

    async def delete_forum_topic(self, chat_id, message_thread_id):
        self.calls.append(("delete_forum_topic", chat_id, message_thread_id))

    async def send_photo(self, chat_id, message_thread_id, photo, caption):
        self.calls.append(("send_photo", caption))

    async def send_document(self, chat_id, message_thread_id, document, caption):
        self.calls.append(("send_document", caption))

    async def get_file(self, file_id):
        self.calls.append(("get_file", file_id)); return SimpleNamespace(file_path="p/" + file_id)

    async def download_file(self, file_path):
        self.calls.append(("download_file", file_path)); return io.BytesIO(b"filebytes")

    async def _close_session(self):
        self.calls.append(("session.close",))


def _tg_with_fake_bot():
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    bot = _FakeBot()
    tg._bot = bot
    return tg, bot


def test_operator_name_prefers_username_then_fullname_then_id():
    assert operator_name(SimpleNamespace(username="support1", full_name="Pyotr", id=7)) == "support1"
    assert operator_name(SimpleNamespace(username=None, full_name="Pyotr Petrov", id=7)) == "Pyotr Petrov"
    assert operator_name(SimpleNamespace(username=None, full_name="", id=7)) == "7"
    assert operator_name(None) == ""


def test_proxy_builds_session_with_connector():
    # proxy set → Bot is built with AiohttpSession over a forward-proxy (egress to Telegram via an external host)
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID, proxy="http://user:secret@proxy.example.com:1080")
    from aiogram.client.session.aiohttp import AiohttpSession
    assert isinstance(tg._bot.session, AiohttpSession)
    assert getattr(tg._bot.session, "_proxy", None) == "http://user:secret@proxy.example.com:1080"


def test_no_proxy_default_session():
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    assert getattr(tg._bot.session, "_proxy", None) in (None, "")


def test_start_polling_registers_handlers(monkeypatch):
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    captured = {}

    async def _fake_poll(*bots, **kw):
        captured["msg_handlers"] = len(tg._dp.message.handlers)
        captured["edit_handlers"] = len(tg._dp.edited_message.handlers)
        captured["handle_signals"] = kw.get("handle_signals")
        captured["close_bot_session"] = kw.get("close_bot_session")

    monkeypatch.setattr(tg._dp, "start_polling", _fake_poll)

    asyncio.run(tg.start_polling(_RecordingHandler()))
    # 3 message handlers (command+media+text) + 1 edited_message handler (edit-sync)
    assert captured == {"msg_handlers": 3, "edit_handlers": 1,
                        "handle_signals": False, "close_bot_session": False}


def _handlers(tg, handler):
    tg._register_handlers(handler)
    return (tg._dp.message.handlers[0].callback,      # [0] command
            tg._dp.message.handlers[2].callback,      # [2] text ([1] is media)
            tg._dp.edited_message.handlers[0].callback)  # edited_message


def test_handlers_route_command_text_edit_delete():
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    h = _RecordingHandler()
    cmd_handler, text_handler, edit_handler = _handlers(tg, h)
    user = SimpleNamespace(username="support1", full_name="Pyotr", id=7)

    # /close → on_command
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/close"),
                            command=SimpleNamespace(command="close")))
    # text → on_message (with message_id)
    asyncio.run(text_handler(SimpleNamespace(message_thread_id=55, from_user=user,
                                             text="check your notification settings", message_id=99)))
    # /del AS A REPLY to message msg_id=42 → on_delete(42); reply not to a service one (forum_topic_created=None)
    reply = SimpleNamespace(message_id=42, forum_topic_created=None)
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/del",
                                            reply_to_message=reply),
                            command=SimpleNamespace(command="del")))
    # /del with no reply → ignored (never delete blindly)
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/del",
                                            reply_to_message=None),
                            command=SimpleNamespace(command="del")))
    # editing message msg_id=42 → on_edit
    asyncio.run(edit_handler(SimpleNamespace(message_thread_id=55, from_user=user,
                                             text="check your notification settings and spam", message_id=42)))

    assert h.commands == [OperatorCommand(55, "/close", "support1")]
    assert h.messages == [OperatorReply(55, "check your notification settings", "support1", 99)]
    assert h.deletes == [OperatorDelete(42)]                 # only a valid reply target
    assert h.edits == [OperatorEdit(42, "check your notification settings and spam")]


def test_clear_routes_to_command_and_help_posts_to_topic():
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    sent = []

    async def _fake_send(thread_id, text):          # /help prints to the topic via self.send_to_topic
        sent.append((thread_id, text))

    tg.send_to_topic = _fake_send
    h = _RecordingHandler()
    cmd_handler, _text, _edit = _handlers(tg, h)
    user = SimpleNamespace(username="support1", full_name="Pyotr", id=7)
    # /clear → routed to on_command (like /close), the relay clears both sides itself
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/clear"),
                            command=SimpleNamespace(command="clear")))
    # /help → the hint is printed to the topic, on_command is NOT called (operator-facing)
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/help"),
                            command=SimpleNamespace(command="help")))
    # /c saludo → forwarded to on_command as "/c saludo" (the arg is carried; the relay expands the preset)
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/c saludo"),
                            command=SimpleNamespace(command="c", args="saludo")))
    # /c with no arg → "/c" (the relay lists the keys)
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/c"),
                            command=SimpleNamespace(command="c", args=None)))
    assert h.commands == [OperatorCommand(55, "/clear", "support1"), OperatorCommand(55, "/c saludo", "support1"),
                          OperatorCommand(55, "/c", "support1")]
    assert sent == [(55, HELP_TEXT)]


def test_handle_media_download_failure_posts_operator_note():
    # arch review #6: a failed download (e.g. file > 20 MB) must not vanish — on_media is NOT called and an
    # operator-facing note is posted into the topic (the user got nothing).
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    notes = []

    async def _fake_send(thread_id, text):
        notes.append((thread_id, text))

    async def _boom(file_id):
        raise RuntimeError("file is too big (getFile 20 MB limit)")

    tg.send_to_topic = _fake_send
    tg._download = _boom
    h = _RecordingHandler()
    tg._register_handlers(h)
    media_handler = tg._dp.message.handlers[1].callback      # [1] is the media handler
    user = SimpleNamespace(username="support1", full_name="", id=1)
    doc = SimpleNamespace(file_id="F1", file_name="big.zip", mime_type="application/zip")
    msg = SimpleNamespace(message_thread_id=55, message_id=7, photo=None, document=doc, caption="", from_user=user)

    asyncio.run(media_handler(msg))                           # must not raise
    assert h.media == []                                      # media dropped, not relayed
    assert notes == [(55, MEDIA_FETCH_FAILED)]                # operator told in the topic


# ── bot wrappers + media success path (arch review: the media path was the other blind spot) ────────────
def test_topic_wrappers_call_the_bot():
    tg, bot = _tg_with_fake_bot()
    assert asyncio.run(tg.ensure_topic("Some long title")) == 4242
    asyncio.run(tg.send_to_topic(7, "hello"))
    asyncio.run(tg.delete_topic(7))
    asyncio.run(tg.close())
    assert [c[0] for c in bot.calls] == ["create_forum_topic", "send_message", "delete_forum_topic", "session.close"]


def test_download_reads_file_bytes():
    tg, bot = _tg_with_fake_bot()
    assert asyncio.run(tg._download("F1")) == b"filebytes"
    assert ("get_file", "F1") in bot.calls and ("download_file", "p/F1") in bot.calls


def test_send_media_photo_vs_document_branching():
    tg, bot = _tg_with_fake_bot()
    asyncio.run(tg.send_media(7, b"x", "a.png", "image/png"))         # inline photo
    asyncio.run(tg.send_media(7, b"x", "a.gif", "image/gif"))         # gif → document (not inline)
    asyncio.run(tg.send_media(7, b"x", "a.svg", "image/svg+xml"))     # svg → document (no inline render)
    asyncio.run(tg.send_media(7, b"x", "a.pdf", "application/pdf"))   # non-image → document
    assert [c[0] for c in bot.calls] == ["send_photo", "send_document", "send_document", "send_document"]


def test_handle_media_photo_and_document_success():
    tg, _bot = _tg_with_fake_bot()
    h = _RecordingHandler()
    tg._register_handlers(h)
    media_handler = tg._dp.message.handlers[1].callback
    user = SimpleNamespace(username="support1", full_name="", id=1)

    # photo branch → largest size, fixed photo.jpg / image/jpeg
    photo_msg = SimpleNamespace(message_thread_id=9, message_id=1, caption="look",
                               photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")],
                               document=None, from_user=user)
    asyncio.run(media_handler(photo_msg))
    # document branch → name/mime taken from the document
    doc = SimpleNamespace(file_id="D1", file_name="report.pdf", mime_type="application/pdf")
    doc_msg = SimpleNamespace(message_thread_id=9, message_id=2, caption="", photo=None, document=doc, from_user=user)
    asyncio.run(media_handler(doc_msg))
    # neither photo nor document → ignored (no on_media)
    none_msg = SimpleNamespace(message_thread_id=9, message_id=3, caption="", photo=None, document=None, from_user=user)
    asyncio.run(media_handler(none_msg))

    assert h.media == [
        OperatorMediaReply(9, b"filebytes", "photo.jpg", "image/jpeg", "look", "support1", 1),
        OperatorMediaReply(9, b"filebytes", "report.pdf", "application/pdf", "", "support1", 2),
    ]
