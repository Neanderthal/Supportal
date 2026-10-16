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
from .widget_api import build_widget_router

logging.basicConfig(level=os.environ.get("RCA_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
_LOG = logging.getLogger("dali.support")


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    store = Store(cfg.db_url)
    store.bootstrap()
    tg = TelegramOps(cfg.tg_bot_token, cfg.tg_group_id, proxy=cfg.tg_proxy)
    relay = Relay(store, tg, cfg.product_callback_url, cfg.callback_secret,
                  outbox_ttl=cfg.outbox_ttl, outbox_max_attempts=cfg.outbox_max_attempts,
                  outbox_backoff_base=cfg.outbox_backoff_base, outbox_backoff_cap=cfg.outbox_backoff_cap)

    def _crash_logger(label: str):
        # Фоновая задача не должна умирать молча (конвенция logging.md): штатная отмена при
        # shutdown — тихо, неожиданный сбой — ERROR с трейсбеком.
        def _cb(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                _LOG.error("[app] фоновая задача %s упала: %s", label, exc, exc_info=exc)
        return _cb

    async def _outbox_loop() -> None:
        """Периодически дорассылать неотданные ответы оператора (outbox повтор). Период — из настроек."""
        while True:
            await asyncio.sleep(cfg.outbox_retry_interval)
            try:
                await relay.retry_outbox_once()
            except Exception:
                _LOG.exception("[outbox] проход повторной доставки дал сбой")

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        poll = asyncio.create_task(tg.start_polling(
            relay.on_operator_message, relay.on_operator_command,
            relay.on_operator_edit, relay.on_operator_delete))
        poll.add_done_callback(_crash_logger("long-polling"))
        outbox = asyncio.create_task(_outbox_loop())
        outbox.add_done_callback(_crash_logger("outbox-retry"))
        _LOG.info("[app] dali-support поднят: long-polling супергруппы + повтор outbox (период %dс)",
                  cfg.outbox_retry_interval)
        try:
            yield
        finally:
            for task in (poll, outbox):
                task.cancel()
            for task in (poll, outbox):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await tg.close()

    app = FastAPI(title="dali-support", version="0.1.0", lifespan=lifespan)
    app.include_router(build_router(relay, cfg.ingress_secret))
    # Публичный widget-API (token-gated) — поверхность браузерного виджета. На облаке доступен через
    # Caddy same-origin `/support-api/v1/widget/*` → без CORS. CORS включаем ТОЛЬКО если задан origin
    # продукта (локальный кросс-оригин dev: чат :8200 → relay :8500).
    app.include_router(build_widget_router(relay, cfg.widget_secret))
    if cfg.allowed_origin:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=[cfg.allowed_origin],
                           allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
    return app


# uvicorn dali_support.app:app — создаём только когда задан токен бота (иначе импорт пакета оффлайн-юнитами
# не должен требовать Telegram-конфиг). Боевой запуск — всегда с заполненным .env.
app = create_app() if os.environ.get("SUPPORT_TG_BOT_TOKEN") else None
