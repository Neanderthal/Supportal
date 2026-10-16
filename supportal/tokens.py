"""Short-lived access token for the browser widget to reach the relay's public widget API.

The browser does NOT hold the HMAC secret and does not name itself: the product (which knows the
logged-in user from its own session) mints the token with `mint_widget_token(user_ref)`, and the relay
verifies it with `verify_widget_token(...)`. The secret `SUPPORT_WIDGET_SECRET` is shared between the
product and the relay (like the ingress/callback contract secrets).

Scheme (SOURCE OF TRUTH — this file; the product mirrors it 1:1 in its own repo — the mint function
lives on the product adapter's side):

    msg   = f"{b64url(user_ref)}.{exp}"          # exp — unix seconds (int), no '=' padding
    sig   = hex(HMAC_SHA256(secret, msg))
    token = f"{msg}.{sig}"                        # three parts joined by '.', all ASCII/URL-safe

Verification: recompute sig over the first two parts, `compare_digest`, then check `exp > now`.
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
    """Signed token `{user_ref, exp}`. Empty secret → empty token (support is disabled)."""
    if not secret:
        return ""
    exp = int(time.time()) + int(ttl)
    msg = f"{_b64url_encode(user_ref)}.{exp}"
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"


def verify_widget_token(token: str, secret: str) -> str | None:
    """Return `user_ref` if the token is valid and not expired, else None. Fail-closed on an empty secret."""
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
