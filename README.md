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
  тот же HMAC на `SUPPORT_CALLBACK_SECRET`. `event ∈ {message, ticket_closed}`. Ответ продукта —
  `{ok, delivered}`: `delivered` = число живых доставок; `0` → адресат оффлайн, relay положит в **outbox** и
  повторит callback с backoff до доставки/TTL. Поля `delivered` нет → считаем доставленным (простой продукт
  без учёта доставки — без вечных повторов). Граница A: relay повторяет HTTP-callback, про сессии продукта не знает.

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
| `SUPPORT_DB_URL` | хранилище тикетов + outbox (дефолт `sqlite:///./dali_support.db`) |
| `SUPPORT_OUTBOX_RETRY_INTERVAL` | период фонового повтора outbox, с (дефолт 30) |
| `SUPPORT_OUTBOX_TTL` | сколько держим неотданный ответ до «сдаёмся», с (дефолт 86400) |
| `SUPPORT_OUTBOX_MAX_ATTEMPTS` / `SUPPORT_OUTBOX_BACKOFF_BASE` / `SUPPORT_OUTBOX_BACKOFF_CAP` | потолок попыток / база и потолок backoff, с (12 / 30 / 1800) |
| `RCA_LOG_LEVEL` | INFO/DEBUG; на INFO/WARNING — только метаданные, не текст сообщений |

## Тесты
```bash
uv run ruff check .
uv run pytest tests/ -q     # оффлайн: стор + HMAC (aiogram/сеть не нужны)
```

## Состояние
**Ф0–Ф2 + Ф3b** реализованы и покрыты оффлайн-тестами (24 теста, ruff чистый):
- **Ф1** ingress + callback: `/v1/messages` (HMAC-гейт), `relay.ingress` (find-or-create тикет → forum-топик →
  отправка), обратный путь оператор→callback (`on_operator_message`/`/close` → подписанный POST, fail-soft).
- **Ф2** Telegram-сторона: `start_polling` (aiogram-хэндлеры топиков супергруппы — `/close`|`/ban`→команда,
  текст→ответ; фильтр «наша группа + внутри топика», неизвестные `/…` игнорируются), `ensure_topic`/`send_to_topic`.
- **Ф3b** outbox повторной доставки: ответ оператора оффлайн-пользователю (callback вернул `delivered=0` /
  упал) ложится в outbox, фоновый цикл повторяет callback с backoff до доставки или TTL. Durability — в
  релее (переиспользуемом), продукт-адаптер остаётся тонким. Граница A сохранена (повтор HTTP, не сессии).

Осталось: **smoke** на тестовой супергруппе + **IPv6-egress на VM** (Telegram с Timeweb достижим только по IPv6,
§9 спеки) · адаптер в rca-chat (Ф3, в репо rca-stack) · развёртывание в `compose.cloud.yml` + CD (Ф4). Не развёрнут.

## Происхождение
Система **ДаЛи** (RCA-диагностика). Разведка рынка решений и обоснование «своего relay» — в rca-stack
(`docs/architecture/support-relay-plan.md`).
