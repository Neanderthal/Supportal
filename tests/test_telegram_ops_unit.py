"""Ф2 — Telegram-сторона оффлайн: operator_name + регистрация/маршрутизация хэндлеров топиков.

aiogram — main-dep (установлен), но сеть не нужна: Bot("123456:abc") конструируется локально
(validate_token проверяет лишь ФОРМАТ токена), start_polling замокан. Проверяем: operator_name;
start_polling регистрирует 2 хэндлера и зовёт polling с handle_signals=False/close_bot_session=False;
/close→on_command и обычный текст→on_message маршрутизируются с верными (thread_id, …, operator).
Реальный end-to-end — ручной smoke на тестовой супергруппе (Ф2 плана).
"""
import asyncio
from types import SimpleNamespace

from dali_support.telegram_ops import TelegramOps, operator_name

_DUMMY_TOKEN = "123456:abc"      # валиден по формату; сеть не трогается до вызова метода бота
_GROUP_ID = -1001234567890


def test_operator_name_prefers_username_then_fullname_then_id():
    assert operator_name(SimpleNamespace(username="support1", full_name="Пётр", id=7)) == "support1"
    assert operator_name(SimpleNamespace(username=None, full_name="Пётр Петров", id=7)) == "Пётр Петров"
    assert operator_name(SimpleNamespace(username=None, full_name="", id=7)) == "7"
    assert operator_name(None) == ""


def test_proxy_builds_session_with_connector():
    # proxy задан → Bot строится с AiohttpSession поверх forward-proxy (egress к Telegram через внешний хост)
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID, proxy="http://dali:secret@85.234.107.135:48610")
    from aiogram.client.session.aiohttp import AiohttpSession
    assert isinstance(tg._bot.session, AiohttpSession)
    assert getattr(tg._bot.session, "_proxy", None) == "http://dali:secret@85.234.107.135:48610"


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
    # 2 message-хэндлера (команда+текст) + 1 edited_message-хэндлер (edit-sync)
    assert captured == {"msg_handlers": 2, "edit_handlers": 1,
                        "handle_signals": False, "close_bot_session": False}


def _handlers(tg, on_message, on_command, on_edit, on_delete):
    tg._register_handlers(on_message, on_command, on_edit, on_delete)
    return (tg._dp.message.handlers[0].callback,      # [0] команда
            tg._dp.message.handlers[1].callback,      # [1] текст
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
    user = SimpleNamespace(username="support1", full_name="Пётр", id=7)

    # /close → on_command
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/close"),
                            command=SimpleNamespace(command="close")))
    # текст → on_message (с message_id)
    asyncio.run(text_handler(SimpleNamespace(message_thread_id=55, from_user=user,
                                             text="проверьте сапун бака", message_id=99)))
    # /del ОТВЕТОМ на сообщение msg_id=42 → on_delete(42); reply не на служебное (forum_topic_created=None)
    reply = SimpleNamespace(message_id=42, forum_topic_created=None)
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/del",
                                            reply_to_message=reply),
                            command=SimpleNamespace(command="del")))
    # /del без ответа → игнор (ничего не удаляем вслепую)
    asyncio.run(cmd_handler(SimpleNamespace(message_thread_id=55, from_user=user, text="/del",
                                            reply_to_message=None),
                            command=SimpleNamespace(command="del")))
    # редактирование сообщения msg_id=42 → on_edit
    asyncio.run(edit_handler(SimpleNamespace(message_thread_id=55, from_user=user,
                                             text="проверьте сапун и уровень", message_id=42)))

    assert seen["cmd"] == [(55, "/close", "support1")]
    assert seen["msg"] == [(55, "проверьте сапун бака", "support1", 99)]
    assert seen["del"] == [42]                               # только валидный reply-таргет
    assert seen["edit"] == [(42, "проверьте сапун и уровень")]
