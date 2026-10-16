"""Ph2 — Telegram side offline: operator_name + topic handler registration/routing.

aiogram is a main dep (installed), but the network is not needed: Bot("123456:abc") is constructed locally
(validate_token checks only the FORMAT of the token), start_polling is mocked. We check: operator_name;
start_polling registers 2 handlers and calls polling with handle_signals=False/close_bot_session=False;
/close→on_command and plain text→on_message are routed with the correct (thread_id, …, operator).
The real end-to-end is a manual smoke test on a test supergroup (plan Ph2).
"""
import asyncio
from types import SimpleNamespace

from supportal.telegram_ops import HELP_TEXT, TelegramOps, operator_name

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

    asyncio.run(tg.start_polling(_noop, _noop, _noop, _noop))
    # 2 message handlers (command+text) + 1 edited_message handler (edit-sync)
    assert captured == {"msg_handlers": 2, "edit_handlers": 1,
                        "handle_signals": False, "close_bot_session": False}


def _handlers(tg, on_message, on_command, on_edit, on_delete):
    tg._register_handlers(on_message, on_command, on_edit, on_delete)
    return (tg._dp.message.handlers[0].callback,      # [0] command
            tg._dp.message.handlers[1].callback,      # [1] text
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
    assert cmds == [(55, "/clear", "support1")]
    assert sent == [(55, HELP_TEXT)]
