"""Конфигурация dali-support (env → дефолт). Секреты — только через окружение/.env, не в коде."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    # Telegram (сторона оператора)
    tg_bot_token: str          # SUPPORT_TG_BOT_TOKEN — секрет
    tg_group_id: int           # SUPPORT_TG_GROUP_ID — forum-супергруппа поддержки (chat_id, обычно отрицательный)
    tg_proxy: str              # SUPPORT_TG_PROXY — forward-proxy для egress к Telegram (пусто → напрямую; при блокировке egress — через внешний IPv4-хост)
    # HTTP-контракт с продуктом
    product_callback_url: str  # SUPPORT_PRODUCT_CALLBACK_URL — куда POST'ить ответы оператора (пусто → модель «прямой виджет», callback дормант)
    callback_secret: str       # SUPPORT_CALLBACK_SECRET — HMAC relay → продукт
    ingress_secret: str        # SUPPORT_INGRESS_SECRET — HMAC продукт → relay
    # Браузерный виджет (прямой доступ к relay через reverse-proxy продукта, same-origin /support-api)
    widget_secret: str         # SUPPORT_WIDGET_SECRET — общий с продуктом секрет подписи токена доступа виджета
    allowed_origin: str        # SUPPORT_ALLOWED_ORIGIN — origin продукта для CORS (локальный кросс-оригин dev; пусто → без CORS, облако same-origin)
    # Хранилище и сеть
    db_url: str                # SUPPORT_DB_URL — по умолчанию локальный SQLite
    http_port: int             # SUPPORT_HTTP_PORT
    # Outbox повторной доставки ответа оператора (продукт вернул delivered=0 / callback упал)
    outbox_retry_interval: int # SUPPORT_OUTBOX_RETRY_INTERVAL — период фонового повтора, с
    outbox_ttl: int            # SUPPORT_OUTBOX_TTL — сколько держим неотданный ответ, с (затем сдаёмся)
    outbox_max_attempts: int   # SUPPORT_OUTBOX_MAX_ATTEMPTS — потолок попыток доставки
    outbox_backoff_base: int   # SUPPORT_OUTBOX_BACKOFF_BASE — базовая задержка backoff, с
    outbox_backoff_cap: int    # SUPPORT_OUTBOX_BACKOFF_CAP — потолок задержки backoff, с

    @staticmethod
    def from_env() -> "Config":
        return Config(
            tg_bot_token=_env("SUPPORT_TG_BOT_TOKEN"),
            tg_group_id=_env_int("SUPPORT_TG_GROUP_ID", 0),
            tg_proxy=_env("SUPPORT_TG_PROXY"),
            product_callback_url=_env("SUPPORT_PRODUCT_CALLBACK_URL"),
            callback_secret=_env("SUPPORT_CALLBACK_SECRET"),
            ingress_secret=_env("SUPPORT_INGRESS_SECRET"),
            widget_secret=_env("SUPPORT_WIDGET_SECRET"),
            allowed_origin=_env("SUPPORT_ALLOWED_ORIGIN"),
            db_url=_env("SUPPORT_DB_URL", "sqlite:///./dali_support.db"),
            http_port=_env_int("SUPPORT_HTTP_PORT", 8500),
            outbox_retry_interval=_env_int("SUPPORT_OUTBOX_RETRY_INTERVAL", 30),
            outbox_ttl=_env_int("SUPPORT_OUTBOX_TTL", 86400),
            outbox_max_attempts=_env_int("SUPPORT_OUTBOX_MAX_ATTEMPTS", 12),
            outbox_backoff_base=_env_int("SUPPORT_OUTBOX_BACKOFF_BASE", 30),
            outbox_backoff_cap=_env_int("SUPPORT_OUTBOX_BACKOFF_CAP", 1800),
        )
