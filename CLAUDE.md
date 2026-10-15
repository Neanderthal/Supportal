# dali-support — relay «веб-чат ↔ оператор в Telegram»

FastAPI-сервис (:8500): пользователь пишет из веб-продукта → оператор отвечает из forum-топика Telegram →
ответ возвращается в живую сессию. Выделен как отдельный проект (образец — `dali-drawing`), т.к.
переиспользуемый и **не зависит от продукта** (знает только `user_ref` + HTTP-callback).

**Полная спека — `docs/architecture/support-relay-plan.md` в репозитории rca-stack** ← начни отсюда.
Локально — [README.md](README.md), [docs/README.md](docs/README.md).

## Команды
- `uv sync && uv run uvicorn dali_support.app:app --port 8500` · `docker compose up -d` · `curl :8500/health`
- `uv run pytest tests/ -q` (оффлайн) · `uv run ruff check .`

## Ключевые факты (не переоткрывать)
- **Граница A-стиля:** relay не знает про Chainlit. Chainlit-специфика (server-push в сессию) — в ТОНКОМ
  адаптере продукта (rca-chat), не здесь. Контракт = 3 HTTP-связки с HMAC (`docs/api.md`).
- **Telegram — long-polling, НЕ webhook:** только egress к api.telegram.org. ⚠️ на облаке Timeweb Telegram
  достижим по IPv6 — контейнеру нужен рабочий IPv6-egress (см. спеку §9).
- **1 топик = 1 тикет пользователя** (forum-супергруппа; бот — админ с правом «Управление темами»).
- **Хранилище:** SQLite по умолчанию (самодостаточно). Postgres — тот же интерфейс `Store` (docs/integration.md).
- **aiogram импортируется лениво** (в `TelegramOps.__init__`) — стор/модели/контракт импортируются без него.

## Конвенции
- Python 3.12, uv, ruff (`[tool.ruff.lint] select` зафиксирован явно, как в rca-stack/dali-drawing).
- Русский без рунглиша в текстах/доках; имена функций/полей/флагов не переводить.
- Логи: на INFO/WARNING — только метаданные (длины/счётчики/ticket_id), НЕ текст сообщений пользователя.
- Секреты — только `.env`/pass, не в коде/настройках-как-код.
