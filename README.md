# dali-support

Встраиваемый relay **«веб-чат пользователя ↔ оператор в Telegram»**: пользователь пишет из веб-продукта,
оператор принимает и отвечает из **forum-топика Telegram**, ответ возвращается в живую сессию пользователя.

Выделен как отдельный проект (по образцу `dali-drawing`), потому что это переиспользуемый кирпич,
не зависящий от конкретного продукта: relay знает только непрозрачный `user_ref` и HTTP-callback — поэтому
встраивается в любой веб-чат тонким адаптером. В системе **ДаЛи** потребитель — Chainlit-чат (:8200).

📖 Полная спека и интеграция с ДаЛи — `docs/architecture/support-relay-plan.md` в репозитории rca-stack.
Локальные документы — [docs/README.md](docs/README.md) (API · интеграция).

## Поток
```
пользователь (веб)                     оператор (Telegram)
      │  текст                                 ▲
      ▼                                        │ ответ в топике
  адаптер продукта ──POST /v1/messages──▶ dali-support ──createForumTopic/sendMessage──▶ forum-супергруппа
      ▲                                        │
      └──── POST callback (ответ) ◀── relay ◀──┘ long-polling getUpdates
```
- **1 топик = 1 активный тикет пользователя.** Оператор отвечает прямо в топике; `/close` закрывает тикет.
- Telegram — через **long-polling** (egress), публичный inbound-webhook не нужен.

## API (контракт встраивания)
- `GET /health` → `{"status":"ok","service":"dali-support"}`.
- `POST /v1/messages` (продукт → relay) — `{user_ref, text, display_name?, meta?}`, заголовок
  `X-Dali-Signature` (HMAC-SHA256 тела на `SUPPORT_INGRESS_SECRET`). Ответ: `{ok, ticket_id, thread_id}`.
- **callback** (relay → продукт, на `SUPPORT_PRODUCT_CALLBACK_URL`) — `{event, user_ref, ticket_id, text, operator}`,
  тот же HMAC на `SUPPORT_CALLBACK_SECRET`. `event ∈ {message, ticket_closed}`.

Детали — [docs/api.md](docs/api.md).

## Запуск
```bash
uv sync
cp .env.example .env     # заполнить токен бота, group_id, секреты, callback-URL
uv run uvicorn dali_support.app:app --port 8500
curl localhost:8500/health
```
Docker: `docker compose up -d` (сервис на :8500).

## Конфигурация (env / .env)
| Переменная | Назначение |
|---|---|
| `SUPPORT_TG_BOT_TOKEN` | токен бота поддержки (@BotFather) |
| `SUPPORT_TG_GROUP_ID` | chat_id forum-супергруппы (бот — админ с «Управление темами») |
| `SUPPORT_PRODUCT_CALLBACK_URL` | куда POST'ить ответы оператора (адаптер продукта) |
| `SUPPORT_CALLBACK_SECRET` / `SUPPORT_INGRESS_SECRET` | HMAC обеих связок |
| `SUPPORT_DB_URL` | хранилище тикетов (дефолт `sqlite:///./dali_support.db`) |
| `RCA_LOG_LEVEL` | INFO/DEBUG; на INFO/WARNING — только метаданные, не текст сообщений |

## Тесты
```bash
uv run ruff check .
uv run pytest tests/ -q     # оффлайн: стор + HMAC (aiogram/сеть не нужны)
```

## Состояние
Скелет фазы **Ф0** (layout, контракт-DTO, стор SQLite, ingress-роут/health, каркас тестов). Telegram-логика
(`start_polling`) и адаптер в rca-chat — фазы Ф1–Ф4 плана (`support-relay-plan.md`). Не развёрнут.

## Происхождение
Система **ДаЛи** (RCA-диагностика). Разведка рынка решений и обоснование «своего relay» — в rca-stack
(`docs/architecture/support-relay-plan.md`).
