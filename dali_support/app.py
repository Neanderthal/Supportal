"""Точка входа ASGI: собрать конфиг/стор/Telegram/relay, поднять FastAPI + фоновый long-polling."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from fastapi import FastAPI

from .api import build_router
from .config import Config
from .relay import Relay
from .store import Store
from .telegram_ops import TelegramOps

logging.basicConfig(level=os.environ.get("RCA_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
_LOG = logging.getLogger("dali.support")


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    store = Store(cfg.db_url)
    store.bootstrap()
    tg = TelegramOps(cfg.tg_bot_token, cfg.tg_group_id)
    relay = Relay(store, tg, cfg.product_callback_url, cfg.callback_secret)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        poll = asyncio.create_task(tg.start_polling(relay.on_operator_message, relay.on_operator_command))

        def _poll_done(task: asyncio.Task) -> None:
            # Фоновая задача не должна умирать молча (конвенция logging.md): штатная отмена при
            # shutdown — тихо, неожиданный сбой polling — ERROR с трейсбеком.
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                _LOG.error("[app] long-polling упал — ответы операторов не доходят: %s", exc, exc_info=exc)

        poll.add_done_callback(_poll_done)
        _LOG.info("[app] dali-support поднят: long-polling операторской супергруппы запущен")
        try:
            yield
        finally:
            poll.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll
            await tg.close()

    app = FastAPI(title="dali-support", version="0.1.0", lifespan=lifespan)
    app.include_router(build_router(relay, cfg.ingress_secret))
    return app


# uvicorn dali_support.app:app — создаём только когда задан токен бота (иначе импорт пакета оффлайн-юнитами
# не должен требовать Telegram-конфиг). Боевой запуск — всегда с заполненным .env.
app = create_app() if os.environ.get("SUPPORT_TG_BOT_TOKEN") else None
