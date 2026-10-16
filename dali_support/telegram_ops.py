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

OnMessage = Callable[[int, str, str, int], Awaitable[None]]  # (thread_id, text, operator, tg_message_id) → None
OnCommand = Callable[[int, str, str], Awaitable[None]]       # (thread_id, command, operator) → None
OnEdit = Callable[[int, str], Awaitable[None]]               # (tg_message_id, new_text) → None
OnDelete = Callable[[int], Awaitable[None]]                  # (tg_message_id) → None

# Команды оператора в топике: /close (закрыть), /del (отозвать сообщение — ответом), /clear (очистить обе
# стороны), /help (подсказка), /ban — задел. Telegram НЕ шлёт ботам событий удаления → /del + edit-sync.
_OPERATOR_COMMANDS = ["close", "ban", "del", "clear", "help"]

# Подсказка оператору (печатается в топик по /help; НЕ уходит пользователю — operator-facing).
HELP_TEXT = (
    "🛠 Команды оператора в этом топике:\n"
    "• Ответить текстом — ответ уйдёт пользователю в чат.\n"
    "• Отредактировать своё сообщение — правка отразится у пользователя.\n"
    "• /del (ответом на сообщение) — убрать это сообщение у пользователя.\n"
    "• /clear — очистить переписку с обеих сторон (у пользователя и здесь; топик будет удалён).\n"
    "• /close — закрыть обращение.\n"
    "⚠️ Удаление сообщения средствами Telegram НЕ синхронизируется с чатом — используйте /del."
)


def operator_name(user) -> str:
    """Подпись оператора для callback/аудита: @username → полное имя → id. Не секрет, не данные пользователя."""
    if user is None:
        return ""
    return user.username or (user.full_name or "") or str(user.id)


class TelegramOps:
    def __init__(self, bot_token: str, group_id: int, proxy: str = "") -> None:
        from aiogram import Bot, Dispatcher
        # proxy задан → egress к api.telegram.org через forward-proxy (на Timeweb Telegram доступен лишь по
        # IPv6; HTTP CONNECT-прокси на внешнем хосте с IPv4-egress снимает блокер, см. docs/integration.md §IPv6).
        if proxy:
            from aiogram.client.session.aiohttp import AiohttpSession
            self._bot = Bot(bot_token, session=AiohttpSession(proxy=proxy))
        else:
            self._bot = Bot(bot_token)
        self._dp = Dispatcher()
        self._group_id = group_id

    async def ensure_topic(self, title: str) -> int:
        """Создать forum-топик в супергруппе → message_thread_id. Вызывается один раз на тикет."""
        topic = await self._bot.create_forum_topic(chat_id=self._group_id, name=title[:128])
        return topic.message_thread_id

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        await self._bot.send_message(chat_id=self._group_id, message_thread_id=thread_id, text=text)

    async def delete_topic(self, thread_id: int) -> None:
        """Удалить forum-топик целиком (все сообщения в нём) — сторона Telegram для /clear. Нужен can_manage_topics."""
        await self._bot.delete_forum_topic(chat_id=self._group_id, message_thread_id=thread_id)

    def _register_handlers(self, on_message: OnMessage, on_command: OnCommand,
                           on_edit: OnEdit, on_delete: OnDelete) -> None:
        """Хэндлеры топиков супергруппы: /del → отзыв (on_delete), /close|/ban → on_command, текст →
        on_message, РЕДАКТИРОВАНИЕ текста → on_edit (edited_message).

        Фильтры: только НАША группа (chat.id == group_id) и только ВНУТРИ топика (message_thread_id задан).
        Команда ловится раньше текста; неизвестные «/…» исключены из текстового хэндлера (не утекают
        пользователю). Сервисные сообщения без `.text` отсеивает фильтр F.text. Telegram НЕ уведомляет об
        удалении → отзыв делаем явной командой `/del` (ответом на сообщение), а правки ловим edited_message.
        """
        from aiogram import F
        from aiogram.filters import Command, CommandObject
        from aiogram.types import Message

        in_our_topic = (F.chat.id == self._group_id) & F.message_thread_id.is_not(None)

        async def _handle_command(message: Message, command: CommandObject) -> None:
            if command.command == "del":
                # Отзыв сообщения: оператор отвечает `/del` на СВО�ё сообщение. Цель — reply_to_message
                # (но не служебное «топик создан»). Нет цели → игнор (ничего не удаляем вслепую).
                rt = message.reply_to_message
                target = rt.message_id if (rt and not rt.forum_topic_created) else 0
                if target:
                    await on_delete(target)
                    _LOG.info("[op] /del thread=%s target_msg=%s", message.message_thread_id, target)
                else:
                    _LOG.info("[op] /del без ответа на сообщение thread=%s — игнор", message.message_thread_id)
                return
            if command.command == "help":
                # Подсказка оператору — печатаем прямо в топик, в ленту пользователю НЕ уходит.
                await self.send_to_topic(message.message_thread_id, HELP_TEXT)
                _LOG.info("[op] /help thread=%s", message.message_thread_id)
                return
            await on_command(message.message_thread_id, f"/{command.command}", operator_name(message.from_user))
            _LOG.info("[op] command=/%s thread=%s", command.command, message.message_thread_id)

        async def _handle_text(message: Message) -> None:
            await on_message(message.message_thread_id, message.text, operator_name(message.from_user),
                             message.message_id)
            _LOG.info("[op] reply thread=%s msg=%s len=%dсимв",
                      message.message_thread_id, message.message_id, len(message.text or ""))

        async def _handle_edit(message: Message) -> None:
            await on_edit(message.message_id, message.text)
            _LOG.info("[op] edit thread=%s msg=%s len=%dсимв",
                      message.message_thread_id, message.message_id, len(message.text or ""))

        self._dp.message.register(_handle_command, in_our_topic, Command(commands=_OPERATOR_COMMANDS))
        self._dp.message.register(_handle_text, in_our_topic, F.text, ~F.text.startswith("/"))
        self._dp.edited_message.register(_handle_edit, in_our_topic, F.text, ~F.text.startswith("/"))

    async def start_polling(self, on_message: OnMessage, on_command: OnCommand,
                            on_edit: OnEdit, on_delete: OnDelete) -> None:
        """Слушать операторов long-polling'ом (getUpdates). Блокирует до отмены задачи. Регистрация
        edited_message-хэндлера → aiogram сам включит этот update-тип в allowed_updates.

        handle_signals=False — polling крутится в event-loop'е uvicorn (не главный «сигнальный» контекст),
        свои обработчики сигналов ставит uvicorn. close_bot_session=False — сессию бота закрывает app
        (lifespan → TelegramOps.close), владение единое.
        """
        self._register_handlers(on_message, on_command, on_edit, on_delete)
        _LOG.info("[tg] long-polling супергруппы group_id=%s — старт", self._group_id)
        await self._dp.start_polling(self._bot, handle_signals=False, close_bot_session=False)

    async def close(self) -> None:
        await self._bot.session.close()
