"""supportal — embeddable relay "web-chat user ↔ support operator in Telegram".

The service does NOT know about any specific product: it speaks via an opaque `user_ref` and an
HTTP callback (see docs/api.md). A chat product plugs in through a thin adapter (docs/integration.md).
"""
from .app import create_app

__all__ = ["create_app"]
