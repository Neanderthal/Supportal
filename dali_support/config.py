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
    # HTTP-контракт с продуктом
    product_callback_url: str  # SUPPORT_PRODUCT_CALLBACK_URL — куда POST'ить ответы оператора
    callback_secret: str       # SUPPORT_CALLBACK_SECRET — HMAC relay → продукт
    ingress_secret: str        # SUPPORT_INGRESS_SECRET — HMAC продукт → relay
    # Хранилище и сеть
    db_url: str                # SUPPORT_DB_URL — по умолчанию локальный SQLite
    http_port: int             # SUPPORT_HTTP_PORT

    @staticmethod
    def from_env() -> "Config":
        return Config(
            tg_bot_token=_env("SUPPORT_TG_BOT_TOKEN"),
            tg_group_id=_env_int("SUPPORT_TG_GROUP_ID", 0),
            product_callback_url=_env("SUPPORT_PRODUCT_CALLBACK_URL"),
            callback_secret=_env("SUPPORT_CALLBACK_SECRET"),
            ingress_secret=_env("SUPPORT_INGRESS_SECRET"),
            db_url=_env("SUPPORT_DB_URL", "sqlite:///./dali_support.db"),
            http_port=_env_int("SUPPORT_HTTP_PORT", 8500),
        )
