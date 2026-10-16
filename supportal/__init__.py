"""supportal — встраиваемый relay «веб-чат пользователя ↔ оператор в Telegram».

Сервис НЕ знает про конкретный продукт: общается непрозрачным `user_ref` и HTTP-callback'ом
(см. docs/api.md). Чат-продукт встраивается тонким адаптером (docs/integration.md).
"""
from .app import create_app

__all__ = ["create_app"]
