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


def test_start_polling_registers_two_handlers(monkeypatch):
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    captured = {}

    async def _fake_poll(*bots, **kw):
        captured["handlers"] = len(tg._dp.message.handlers)
        captured["handle_signals"] = kw.get("handle_signals")
        captured["close_bot_session"] = kw.get("close_bot_session")

    monkeypatch.setattr(tg._dp, "start_polling", _fake_poll)

    async def _noop(*a):
        return None

    asyncio.run(tg.start_polling(_noop, _noop))
    assert captured == {"handlers": 2, "handle_signals": False, "close_bot_session": False}


def test_handlers_route_command_and_text():
    tg = TelegramOps(_DUMMY_TOKEN, _GROUP_ID)
    seen = {"msg": [], "cmd": []}

    async def on_message(thread_id, text, operator):
        seen["msg"].append((thread_id, text, operator))

    async def on_command(thread_id, command, operator):
        seen["cmd"].append((thread_id, command, operator))

    tg._register_handlers(on_message, on_command)
    # порядок регистрации: [0] команда, [1] текст
    cmd_handler = tg._dp.message.handlers[0].callback
    text_handler = tg._dp.message.handlers[1].callback

    user = SimpleNamespace(username="support1", full_name="Пётр", id=7)
    cmd_msg = SimpleNamespace(message_thread_id=55, from_user=user, text="/close")
    text_msg = SimpleNamespace(message_thread_id=55, from_user=user, text="проверьте сапун бака")

    asyncio.run(cmd_handler(cmd_msg, command=SimpleNamespace(command="close")))
    asyncio.run(text_handler(text_msg))

    assert seen["cmd"] == [(55, "/close", "support1")]
    assert seen["msg"] == [(55, "проверьте сапун бака", "support1")]
