"""Обёртка над Telegram Bot API (aiogram 3): forum-топики + long-polling стороны оператора.

Выбор long-polling (getUpdates), НЕ webhook: relay не нуждается в публично доступном inbound-эндпоинте —
только egress к api.telegram.org (на облаке — по IPv6, см. docs/integration.md). Примитивы сверены по
официальной документации Bot API: createForumTopic → message_thread_id; sendMessage(..., message_thread_id=…).

aiogram импортируется ЛЕНИВО в __init__ — пакет остаётся импортируемым (стор/модели/контракт) без
установленного aiogram (оффлайн-юниты не требуют его).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

_LOG = logging.getLogger("dali.support.tg")

OnMessage = Callable[[int, str, str], Awaitable[None]]   # (thread_id, text, operator) → None
OnCommand = Callable[[int, str, str], Awaitable[None]]   # (thread_id, command, operator) → None


class TelegramOps:
    def __init__(self, bot_token: str, group_id: int) -> None:
        from aiogram import Bot, Dispatcher
        self._bot = Bot(bot_token)
        self._dp = Dispatcher()
        self._group_id = group_id

    async def ensure_topic(self, title: str) -> int:
        """Создать forum-топик в супергруппе → message_thread_id. Вызывается один раз на тикет."""
        topic = await self._bot.create_forum_topic(chat_id=self._group_id, name=title[:128])
        return topic.message_thread_id

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        await self._bot.send_message(chat_id=self._group_id, message_thread_id=thread_id, text=text)

    async def start_polling(self, on_message: OnMessage, on_command: OnCommand) -> None:
        """Слушать ответы операторов в топиках супергруппы; маршрутизация по message_thread_id.

        СКЕЛЕТ (фаза Ф2): здесь регистрируются хэндлеры aiogram — фильтр по chat.id == group_id и наличию
        message_thread_id; команды /close|/ban → on_command, обычный текст → on_message(thread_id, text,
        operator) — и запускается self._dp.start_polling(self._bot). Детали — §13 Ф2 плана.
        """
        raise NotImplementedError("Регистрация aiogram-хэндлеров + dp.start_polling — фаза Ф2 (см. план).")

    async def close(self) -> None:
        await self._bot.session.close()
