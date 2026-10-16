"""Ph2 — Telegram side offline: operator_name + topic handler registration/routing.

aiogram is a main dep (installed), but the network is not needed: Bot("123456:abc") is constructed locally
(validate_token checks only the FORMAT of the token), start_polling is mocked. We check: operator_name;
start_polling registers 2 handlers and calls polling with handle_signals=False/close_bot_session=False;
/close→on_command and plain text→on_message are routed with the correct (thread_id, …, operator).
The real end-to-end is a manual smoke test on a test supergroup (plan Ph2).
"""
import asyncio
from types import SimpleNamespace

from supportal.telegram_ops import HELP_TEXT, MEDIA_FETCH_FAILED, TelegramOps, operator_name

_DUMMY_TOKEN = "123456:abc"      # valid by format; the network is not touched until a bot method is called
_GROUP_ID = -1001234567890


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

    async def _noop(*a):
        return None

    asyncio.run(tg.start_polling(_noop, _noop, _noop, _noop, _noop))
    # 3 message handlers (command+media+text) + 1 edited_message handler (edit-sync)
    assert captured == {"msg_handlers": 3, "edit_handlers": 1,
                        "handle_signals": False, "close_bot_session": False}


def _handlers(tg, on_message, on_command, on_edit, on_delete):
    async def _media(*a):
        return None
    tg._register_handlers(on_message, on_command, on_edit, on_delete, _media)
    return (tg._dp.message.handlers[0].callback,      # [0] command
            tg._dp.message.handlers[2].callback,      # [2] text ([1] is media)
            tg._dp.edited_message.handlers[0].callback)  # edited_message


def test_handlers_route_command_text_edit_delete():
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    seen = {"msg": [], "cmd": [], "edit": [], "del": []}

    async def on_message(thread_id, text, operator, tg_message_id):
        seen["msg"].append((thread_id, text, operator, tg_message_id))

    async def on_command(thread_id, command, operator):
        seen["cmd"].append((thread_id, command, operator))

    async def on_edit(tg_message_id, text):
        seen["edit"].append((tg_message_id, text))

    async def on_delete(tg_message_id):
        seen["del"].append(tg_message_id)

    cmd_handler, text_handler, edit_handler = _handlers(tg, on_message, on_command, on_edit, on_delete)
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

    assert seen["cmd"] == [(55, "/close", "support1")]
    assert seen["msg"] == [(55, "check your notification settings", "support1", 99)]
    assert seen["del"] == [42]                               # only a valid reply target
    assert seen["edit"] == [(42, "check your notification settings and spam")]


def test_clear_routes_to_command_and_help_posts_to_topic():
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    sent = []

    async def _fake_send(thread_id, text):          # /help prints to the topic via self.send_to_topic
        sent.append((thread_id, text))

    tg.send_to_topic = _fake_send
    cmds = []

    async def on_command(thread_id, command, operator):
        cmds.append((thread_id, command, operator))

    async def _noop(*a):
        return None

    cmd_handler, _text, _edit = _handlers(tg, _noop, on_command, _noop, _noop)
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
    assert cmds == [(55, "/clear", "support1"), (55, "/c saludo", "support1"), (55, "/c", "support1")]
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
    media_calls = []

    async def on_media(*a):
        media_calls.append(a)

    async def _noop(*a):
        return None

    tg._register_handlers(_noop, _noop, _noop, _noop, on_media)
    media_handler = tg._dp.message.handlers[1].callback      # [1] is the media handler
    user = SimpleNamespace(username="support1", full_name="", id=1)
    doc = SimpleNamespace(file_id="F1", file_name="big.zip", mime_type="application/zip")
    msg = SimpleNamespace(message_thread_id=55, message_id=7, photo=None, document=doc, caption="", from_user=user)

    asyncio.run(media_handler(msg))                           # must not raise
    assert media_calls == []                                  # media dropped, not relayed
    assert notes == [(55, MEDIA_FETCH_FAILED)]                # operator told in the topic
