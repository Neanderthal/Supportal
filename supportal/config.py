"""supportal configuration (env → default). Secrets come only from the environment/.env, never the code."""
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
    # Telegram (operator side)
    tg_bot_token: str          # SUPPORT_TG_BOT_TOKEN — secret
    tg_group_id: int           # SUPPORT_TG_GROUP_ID — support forum supergroup (chat_id, usually negative)
    tg_proxy: str              # SUPPORT_TG_PROXY — forward-proxy for egress to Telegram (empty → direct; when egress is blocked — via an external IPv4 host)
    # HTTP contract with the product
    product_callback_url: str  # SUPPORT_PRODUCT_CALLBACK_URL — where to POST operator replies (empty → "direct widget" model, callback dormant)
    callback_secret: str       # SUPPORT_CALLBACK_SECRET — HMAC relay → product
    ingress_secret: str        # SUPPORT_INGRESS_SECRET — HMAC product → relay
    # Browser widget (direct access to the relay via the product's reverse-proxy, same-origin /support-api)
    widget_secret: str         # SUPPORT_WIDGET_SECRET — secret shared with the product for signing the widget access token
    allowed_origin: str        # SUPPORT_ALLOWED_ORIGIN — product origin for CORS (local cross-origin dev; empty → no CORS, same-origin in the cloud)
    # Storage and network
    db_url: str                # SUPPORT_DB_URL — local SQLite by default
    http_port: int             # SUPPORT_HTTP_PORT
    # Outbox for retrying operator-reply delivery (product returned delivered=0 / callback failed)
    outbox_retry_interval: int # SUPPORT_OUTBOX_RETRY_INTERVAL — background retry period, s
    outbox_ttl: int            # SUPPORT_OUTBOX_TTL — how long we keep an undelivered reply, s (then give up)
    outbox_max_attempts: int   # SUPPORT_OUTBOX_MAX_ATTEMPTS — delivery attempt ceiling
    outbox_backoff_base: int   # SUPPORT_OUTBOX_BACKOFF_BASE — base backoff delay, s
    outbox_backoff_cap: int    # SUPPORT_OUTBOX_BACKOFF_CAP — backoff delay ceiling, s

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
            db_url=_env("SUPPORT_DB_URL", "sqlite:///./supportal.db"),
            http_port=_env_int("SUPPORT_HTTP_PORT", 8500),
            outbox_retry_interval=_env_int("SUPPORT_OUTBOX_RETRY_INTERVAL", 30),
            outbox_ttl=_env_int("SUPPORT_OUTBOX_TTL", 86400),
            outbox_max_attempts=_env_int("SUPPORT_OUTBOX_MAX_ATTEMPTS", 12),
            outbox_backoff_base=_env_int("SUPPORT_OUTBOX_BACKOFF_BASE", 30),
            outbox_backoff_cap=_env_int("SUPPORT_OUTBOX_BACKOFF_CAP", 1800),
        )
