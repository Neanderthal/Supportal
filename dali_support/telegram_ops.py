"""Обёртка над Telegram Bot API (aiogram 3): forum-топики + long-polling стороны оператора.

Выбор long-polling (getUpdates), НЕ webhook: relay не нуждается в публично доступном inbound-эндпоинте —
только egress к api.telegram.org (на облаке — по IPv6, см. docs/integration.md). Примитивы сверены по
официальной документации Bot API: createForumTopic → message_thread_id; sendMessage(..., message_thread_id=…).

aiogram импортируется ЛЕНИВО в __init__/_register_handlers — пакет остаётся импортируемым (стор/модели/
контракт) без установленного aiogram (оффлайн-юниты не требуют его).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

_LOG = logging.getLogger("dali.support.tg")

OnMessage = Callable[[int, str, str], Awaitable[None]]   # (thread_id, text, operator) → None
OnCommand = Callable[[int, str, str], Awaitable[None]]   # (thread_id, command, operator) → None

# Команды оператора в топике, которые relay понимает. v1: только /close; /ban — задел на будущее.
_OPERATOR_COMMANDS = ["close", "ban"]


def operator_name(user) -> str:
    """Подпись оператора для callback/аудита: @username → полное имя → id. Не секрет, не данные пользователя."""
    if user is None:
        return ""
    return user.username or (user.full_name or "") or str(user.id)


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

    def _register_handlers(self, on_message: OnMessage, on_command: OnCommand) -> None:
        """Хэндлеры топиков супергруппы: /close|/ban → on_command, обычный текст → on_message.

        Фильтры: только НАША группа (chat.id == group_id) и только ВНУТРИ топика (message_thread_id задан).
        Команда ловится раньше текста; неизвестные «/…» исключены из текстового хэндлера (не утекают
        пользователю как ответ оператора). Сервисные сообщения (создание топика и т.п.) без `.text`
        отсеивает фильтр F.text.
        """
        from aiogram import F
        from aiogram.filters import Command, CommandObject
        from aiogram.types import Message

        in_our_topic = (F.chat.id == self._group_id) & F.message_thread_id.is_not(None)

        async def _handle_command(message: Message, command: CommandObject) -> None:
            await on_command(message.message_thread_id, f"/{command.command}", operator_name(message.from_user))
            _LOG.info("[op] command=/%s thread=%s", command.command, message.message_thread_id)

        async def _handle_text(message: Message) -> None:
            await on_message(message.message_thread_id, message.text, operator_name(message.from_user))
            _LOG.info("[op] reply thread=%s len=%dсимв", message.message_thread_id, len(message.text or ""))

        self._dp.message.register(_handle_command, in_our_topic, Command(commands=_OPERATOR_COMMANDS))
        self._dp.message.register(_handle_text, in_our_topic, F.text, ~F.text.startswith("/"))

    async def start_polling(self, on_message: OnMessage, on_command: OnCommand) -> None:
        """Слушать ответы операторов long-polling'ом (getUpdates). Блокирует до отмены задачи.

        handle_signals=False — polling крутится в event-loop'е uvicorn (не главный «сигнальный» контекст),
        свои обработчики сигналов ставит uvicorn. close_bot_session=False — сессию бота закрывает app
        (lifespan → TelegramOps.close), владение единое.
        """
        self._register_handlers(on_message, on_command)
        _LOG.info("[tg] long-polling супергруппы group_id=%s — старт", self._group_id)
        await self._dp.start_polling(self._bot, handle_signals=False, close_bot_session=False)

    async def close(self) -> None:
        await self._bot.session.close()
