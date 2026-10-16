"""Короткоживущий токен доступа браузерного виджета к публичному widget-API relay.

Браузер НЕ держит HMAC-секрет и не называет себя сам: продукт (знает залогиненного по своей сессии)
чеканит токен `mint_widget_token(user_ref)`, relay проверяет `verify_widget_token(...)`. Секрет
`SUPPORT_WIDGET_SECRET` общий у продукта и relay (как ingress/callback-секреты контракта).

Схема (ИСТОЧНИК ПРАВДЫ — этот файл; продукт на другом репо повторяет её 1:1, см.
rca-chat/support_bridge.py `mint_widget_token`):

    msg   = f"{b64url(user_ref)}.{exp}"          # exp — unix-секунды (int), без паддинга '='
    sig   = hex(HMAC_SHA256(secret, msg))
    token = f"{msg}.{sig}"                        # три части через '.', всё ASCII/URL-safe

Проверка: пересобрать sig над первыми двумя частями, `compare_digest`, затем `exp > now`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time


def _b64url_encode(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _b64url_decode(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def mint_widget_token(user_ref: str, secret: str, ttl: int = 900) -> str:
    """Подписанный токен `{user_ref, exp}`. Пустой секрет → пустой токен (поддержка выключена)."""
    if not secret:
        return ""
    exp = int(time.time()) + int(ttl)
    msg = f"{_b64url_encode(user_ref)}.{exp}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def verify_widget_token(token: str, secret: str) -> str | None:
    """Вернуть `user_ref`, если токен валиден и не просрочен, иначе None. Fail-closed на пустом секрете."""
    if not secret or not token:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    ref_b64, exp_s, sig = parts
    msg = f"{ref_b64}.{exp_s}"
    expected = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        if int(exp_s) <= int(time.time()):
            return None
        return _b64url_decode(ref_b64)
    except (ValueError, UnicodeDecodeError):
        return None
