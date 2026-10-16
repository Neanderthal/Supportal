"""ASGI entry point: assemble config/store/Telegram/relay, start FastAPI + a background long-polling loop.

⚠️ SINGLE-PROCESS SERVICE (hard invariant). The Telegram long-poller, the outbox-retry loop and the
in-memory rate limiter (ratelimit.py) are per-process singletons. Run EXACTLY ONE process: do NOT start
uvicorn with `--workers >1` and do NOT run multiple replicas. Two processes mean two `getUpdates` consumers
(Telegram answers one with 409 Conflict — replies silently split), double outbox delivery and per-worker
(not global) rate limits. Swapping the SQLite store for Postgres does NOT lift this limit by itself —
horizontal scale-out additionally needs the poller/outbox to run as a single leader. See
docs/architecture.md (§Scaling) and docs/integration.md (§Deploying).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os

from fastapi import FastAPI

from .api import build_router
from .config import Config
from .flags import Flags
from .relay import Relay
from .settings import get_bool
from .store import Store
from .telegram_ops import TelegramOps
from .widget_api import build_widget_router

# OpenAPI tag descriptions — group the routes in /docs by contour (see docs/api.md).
_OPENAPI_TAGS = [
    {"name": "widget", "description": "Contour A — the browser widget (Bearer token minted by the product)."},
    {"name": "product", "description": "Contour B — the product backend (HMAC `X-Supportal-Signature`)."},
    {"name": "health", "description": "Liveness / readiness."},
]

logging.basicConfig(level=os.environ.get("SUPPORT_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
_LOG = logging.getLogger("supportal")


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or Config.from_env()
    store = Store(cfg.db_url)
    store.bootstrap()
    tg = TelegramOps(cfg.tg_bot_token, cfg.tg_group_id, proxy=cfg.tg_proxy)
    relay = Relay(store, tg, cfg.product_callback_url, cfg.callback_secret,
                  outbox_ttl=cfg.outbox_ttl, outbox_max_attempts=cfg.outbox_max_attempts,
                  outbox_backoff_base=cfg.outbox_backoff_base, outbox_backoff_cap=cfg.outbox_backoff_cap)

    def _crash_logger(label: str):
        # A background task must not die silently (logging.md convention): a normal cancellation at
        # shutdown — quietly; an unexpected failure — ERROR with a traceback.
        def _cb(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                _LOG.error("[app] background task %s crashed: %s", label, exc, exc_info=exc)
        return _cb

    async def _outbox_loop() -> None:
        """Periodically re-deliver undelivered operator replies (outbox retry). Period — from settings."""
        while True:
            await asyncio.sleep(cfg.outbox_retry_interval)
            try:
                await relay.retry_outbox_once()
            except Exception:
                _LOG.exception("[outbox] retry delivery pass failed")

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        poll = asyncio.create_task(tg.start_polling(relay))   # relay implements OperatorHandler
        poll.add_done_callback(_crash_logger("long-polling"))
        outbox = asyncio.create_task(_outbox_loop())
        outbox.add_done_callback(_crash_logger("outbox-retry"))
        _LOG.info("[app] supportal up — SINGLE-PROCESS: one long-poller + one outbox loop + in-memory rate "
                  "limiter. Do NOT run --workers>1 or multiple replicas (409 Conflict / double delivery). "
                  "Outbox retry period %ds.", cfg.outbox_retry_interval)
        try:
            yield
        finally:
            for task in (poll, outbox):
                task.cancel()
            for task in (poll, outbox):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await tg.close()
            store.close_connection()

    # Feature SUPPORT_OPENAPI (default off): expose the Swagger UI (/docs), ReDoc (/redoc) and the schema
    # (/openapi.json). Off by default — the relay usually sits behind the product's reverse-proxy and the API
    # surface need not be public. Read at startup (not hot): flip it in .env/settings.yaml, then restart.
    docs_on = get_bool(Flags.OPENAPI, False)
    app = FastAPI(
        title="supportal", version="0.1.0", lifespan=lifespan,
        summary="Embeddable support relay — a browser widget ↔ Telegram operator topics.",
        openapi_tags=_OPENAPI_TAGS,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )
    app.include_router(build_router(relay, cfg.ingress_secret))
    # Public widget API (token-gated) — the browser widget's surface. In production it's reached via the
    # product's reverse-proxy (same-origin `/support-api/v1/widget/*`) → no CORS. We enable CORS ONLY
    # when a product origin is set (local cross-origin dev: chat → relay :8500).
    app.include_router(build_widget_router(relay, cfg.widget_secret))
    if cfg.allowed_origin:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=[cfg.allowed_origin],
                           allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])
    return app


# uvicorn supportal.app:app — created only when a bot token is set (otherwise importing the package from
# offline unit tests must not require Telegram config). A production run always has a filled-in .env.
app = create_app() if os.environ.get("SUPPORT_TG_BOT_TOKEN") else None
