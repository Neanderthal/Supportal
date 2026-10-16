> **Мини-спека: релей «веб-чат поддержки → оператор в Telegram» (`dali-support`).** Пользователь пишет в
> поддержку прямо из веб-чата продукта, оператор принимает и отвечает из Telegram, ответ возвращается в ту
> же живую сессию. **Аудитория: внутренний.** Дата: 2026-10-15 · **ревизия 2026-10-16** (поддержка вынесена
> в встраиваемый виджет-бандл, браузер общается с relay напрямую — см. §5/§8). Код релея и весь UI виджета —
> этот проект `dali-support/`; интеграционная сторона (адаптер продукта, reverse-proxy-маршрут, развёртывание
> — §8/§11) пишется на стороне встраивающего продукта.

# Мини-спека: релей «веб-чат поддержки → оператор в Telegram»

## 1. Форма и границы задачи

Поток: **пользователь (веб) → relay → forum-топик оператора (Telegram) → relay → живая сессия пользователя.**

**Почему свой relay, а не готовый продукт** (вывод разведки рынка, сверено по первоисточникам):
рынок закрывает «обратную» схему — пользователь пишет в Telegram, оператор сидит в веб-консоли вендора
(Chatwoot, Jivo, Crisp, Chaport, Tawk.to, RU-агрегаторы). Наша схема «пользователь в вебе, оператор в
Telegram» — нишевая: её держат лишь мелкий SaaS (Human Reply, Chatgram — egress данных третьей стороне,
в Германию) и отдельные OSS-приложения/виджеты (LiveHelperChat, Intergram — своё приложение/пузырь, не в
нашем треде). Свой relay кладёт обращения **в тот же канал продукта**, переиспользует собственную
инфраструктуру (Telegram-бот, при необходимости прокси-мост egress, кастом-маршруты продукта), ничего не
отдаёт наружу. [verified]

**Non-goals (v1):** тикет-очередь/SLA/аналитика, несколько продуктов, вложения/медиа, несколько операторов
на тикет, «оператор печатает»/присутствие, SSE вместо polling. Каждое — отдельная фаза, не блокирует v1.
(Персистентность и восстановление переписки — уже реализованы в ревизии 2026-10-16, таблица `messages`, §6.)

## 2. Разделение кода (ключевое требование — «код максимально отдельно»)

```
браузер пользователя (страница продукта)
│  виджет-бандл dali-support (widget.js/css, вендорен в статику продукта, подключён его загрузочным скриптом):
│   • кнопка 💬 + панель справа + лента + ввод (ванильный JS, ноль зависимостей от фреймворков)
│   • polling GET /support-api/v1/widget/messages · POST …/send (Bearer token)
│        │ POST /support/config (сессия продукта → token)  │ /support-api/v1/widget/* (Bearer)
│        ▼                                                 ▼
┌──── продукт (веб-чат) ───────┐                 ┌──── reverse-proxy (same-origin) ────┐
│ адаптер продукта (тонкий):   │                 │ /support-api/* → strip →             │
│  POST /support/config → токен│                 │ dali-support:8500 (без CORS)         │
└──────────────────────────────┘                 └───────────────┬──────────────────────┘
                                                                  ▼
┌──────────── отдельный проект dali-support/ (FastAPI :8500) ────────────────┐
│  widget_api.py /v1/widget/{send,messages} (token)   relay.py  оркестрация   │
│  tokens.py verify   store.py тикет+лента (SQLite)   telegram_ops.py aiogram  │
└───────────────────────────────────┬────────────────────────────────────────┘
                       long-polling  │ getUpdates (egress; при блокировке — прокси)
                                     ▼  Telegram: forum-супергруппа (1 топик = 1 пользователь)
```

- **В отдельном проекте** `dali-support/` (свой `pyproject`/`Dockerfile`/тесты, порт **8500**): всё про
  Telegram, тикеты, ленту, токен и HTTP-контракт **+ весь UI виджета** (`dali_support/widget/`).
  **Ноль зависимости от фреймворка продукта** → виджет встраивается в любой веб-продукт одним включением бандла.
- **В продукте** (тонкий адаптер): ровно одно — чеканка токена залогиненному (`POST /support/config`) +
  вендоринг/отдача бандла. Нет UI-элемента, server-push, реестра сессий. Это **«граница A»**: логика и UI
  снаружи, продукт даёт только identity.

## 3. Потоки

(Ревизия 2026-10-16 — браузерный виджет напрямую в relay, polling.)

**Старт:** бандл виджета (из продукта) делает `POST /support/config` → токен (identity из сессии продукта).
Нет токена → виджет не поднимается. Сразу `GET /v1/widget/messages?since=0` → восстановление ленты.

**Исходящий (пользователь → оператор):**
1. Пользователь пишет в панель виджета → `POST {api_base}/v1/widget/send {text}` (Bearer token, через
   reverse-proxy `/support-api`). relay проверяет токен → `user_ref`.
2. Relay: find-or-create тикет → `ensure_topic` (один раз) → `sendMessage(message_thread_id=…)` в супергруппу;
   пишет строку `in` в ленту `messages`. Возвращает `{message_id}` (виджет эхо-рисует, дедуп по id).

**Входящий (оператор → пользователь):**
3. Оператор отвечает **в топике**. Relay ловит long-polling'ом, по `message_thread_id` → тикет → `user_ref`;
   пишет строку `out` в ленту `messages`.
4. Виджет очередным `GET /v1/widget/messages?since=<cursor>` (~4с) забирает новое → рисует; при закрытой
   панели — бейдж. Оффлайн-пользователь → строка persistentна, подтянется при следующем заходе (`since=0`).

## 4. Отдельный проект `dali-support/`

```
dali-support/
├── README.md · CONTRIBUTING.md · docs/{README,api,integration}.md
├── pyproject.toml · Dockerfile · compose.yml · .env.example
├── dali_support/
│   ├── config.py        # env → Config (секреты только из окружения)
│   ├── models.py        # DTO контракта (IngressMessage/IngressResult/OperatorEvent) — граница встраивания
│   ├── store.py         # Store: тикет↔топик↔user + лента messages (tg_message_id/seq/deleted), SQLite
│   ├── telegram_ops.py  # TelegramOps: aiogram forum-топики + long-polling (текст/команды/edited_message)
│   ├── tokens.py        # mint/verify короткоживущего токена доступа виджета (HMAC+exp) — схема общая с продуктом
│   ├── relay.py         # Relay: ingress/on_operator_message/on_operator_edit/on_operator_delete/changes_since
│   ├── api.py           # FastAPI router: POST /v1/messages (HMAC, контур B) + GET /health
│   ├── widget_api.py    # публичный widget-API: /v1/widget/{send,messages} (Bearer token) — контур A
│   ├── widget/          # встраиваемый бандл UI: widget.js + widget.css (ванильный, вендорится в продукт)
│   └── app.py           # сборка + lifespan (long-polling) + widget-router + опц. CORS
└── tests/   # оффлайн: контракт/стор/HMAC + test_widget_unit (токен/лента/edit-del/widget-API)
```
Зависимости: `fastapi`, `uvicorn`, `aiogram>=3.13`, `httpx`, `pydantic`. Ruff-набор зафиксирован явно.

**Long-polling, а не webhook (решение):** relay не нуждается в публично доступном inbound-эндпоинте для
Telegram — только egress к `api.telegram.org`. Меньше публичной поверхности, не трогаем reverse-proxy
продукта. Цена — долгоживущий egress (см. §9, IPv6).

## 5. HTTP-контракт (ревизия 2026-10-16 — браузерный виджет напрямую в relay)

> **Ревизия архитектуры (2026-10-16).** Поддержка вынесена в **встраиваемый виджет-бандл** `dali-support`
> (`widget.js`/`widget.css`, ванильный JS). Браузер общается с relay **напрямую** (не через server-push
> продукта): через reverse-proxy продукта same-origin путь `/support-api/v1/widget/*` → `dali-support:8500`
> (strip-prefix), **без CORS**. Доставка ответов оператора — **polling** (не callback). Прежняя модель
> server-push/callback (§8-эскиз ниже, outbox модель B) **заменяется** и остаётся кодом за конфигом (relay
> шлёт callback, только если задан `SUPPORT_PRODUCT_CALLBACK_URL`; в модели «виджет» пуст → дормант).

Авторизация браузера — **короткоживущий токен** `{user_ref, exp}`, подписанный общим секретом
`SUPPORT_WIDGET_SECRET` (`dali_support/tokens.py` — источник схемы; продукт чеканит, relay проверяет).
`user_ref` берётся С СЕРВЕРА (нормализованный ключ identity из сессии продукта) — анти-спуфинг.

**1. Виджет → продукт** `POST /support/config` (идентификация по сессии продукта):
```json
→ 200 {"api_base":"/support-api","token":"<b64url(user_ref)>.<exp>.<hmac>","ttl":900}   // 401 — не залогинен; 404 — поддержка выключена
```
**2. Виджет → relay** (Bearer token; через reverse-proxy `/support-api`):
```
POST /v1/widget/send   {"text":"Не приходит письмо для сброса пароля"}  → 200 {"ok":true,"message_id":12}
GET  /v1/widget/messages?since=<seq> → 200 {"messages":[{"id":12,"direction":"in|out","author":"…","text":"…","ts":…,"deleted":false}],"cursor":17}
```
`messages` — изменения ленты с `seq>since` (новые + отредактированные + удалённые; `since=0` → вся лента).
`cursor` = max seq ответа → в `?since=` следующего опроса. Виджет сверяет по `id`: новое → добавить; видел →
обновить текст (edit-sync); `deleted:true` → убрать пузырь. 401 — нет/просрочен/подделан токен. Ingress
`/v1/messages` (HMAC продукт→relay) остаётся для модели «через продукт», но наружу **НЕ выводится**.

**3. Telegram → relay** — внутренний long-polling, эндпоинта у продукта нет.

## 6. Модель данных (SQLite дефолт)

```sql
tickets(ticket_id PK, user_ref, display_name, thread_id UNIQUE, status 'open|closed', created_at, last_at)
messages(id PK AUTOINCREMENT, ticket_id, user_ref, direction 'in|out', author, text, created_at,
         tg_message_id, deleted, seq)   -- лента; tg_message_id → edit/del; deleted → мягкое удаление; seq → курсор
outbox(id PK AUTOINCREMENT, ticket_id, user_ref, event, text, operator, status 'pending|delivered|dead',
       attempts, created_at, next_attempt_at)   -- контур B: повтор callback оффлайн-пользователю (§13 Ф3b)
```
- `tickets`: `get_open_by_user` / `get_by_thread` — один активный топик на пользователя + обратный маршрут.
- **`messages`** (ревизия 2026-10-16): источник правды ленты для polling, восстановления и edit/del.
  `add_message(...)` на ingress (in) и ответ оператора (out, с `tg_message_id`); `update_message_by_tg`/
  `delete_message_by_tg` — edit-sync и `/del`; `changes_since(user_ref, after_seq)` — выдача виджету.
  **`seq`** (монотонный, растёт на insert/edit/delete) = курсор polling → правки/удаления старых строк тоже
  доходят до виджета. Персистентна → ответ оффлайн-пользователю подтянется при след. опросе.
- Топик/лента нельзя терять между рестартами → персистентны. Многопользовательский контур → Postgres
  с тем же интерфейсом `Store` (docs/integration.md подпроекта).

## 7. Telegram-сторона

- Настройка (один раз): супергруппа с включёнными **Topics**, бот — администратор с правом **«Управление
  темами»** (can_manage_topics); `chat_id` (обычно `-100…`) → `SUPPORT_TG_GROUP_ID`.
- **1 топик = 1 активный тикет пользователя.** Заголовок топика = `display_name`. Команды оператора:
  `/close` — закрыть тикет; **`/clear`** — очистить обе стороны (вся лента → `deleted` + **удаление топика**
  `deleteForumTopic` + сброс тикета → следующее сообщение создаст свежий топик); **`/help`** — бот печатает
  подсказку по командам прямо в топик (operator-facing, в ленту пользователю не уходит).
- **Правка/отзыв ответа (ревизия 2026-10-16).** ⚠️ Telegram Bot API **не шлёт ботам событий удаления**
  (`deleted_message` для обычного бота в группе нет — только `deleted_business_messages` у Business-ботов) →
  нативное удаление в Telegram виджет синхронизировать НЕ может. Вместо этого: **редактирование** сообщения
  оператором ловится `edited_message` → текст правится в ленте (edit-sync); **отзыв** — команда **`/del`**
  ответом на своё сообщение → строка помечается `deleted`. Оба повышают `seq` → доходят до виджета polling-ом.
- Примитивы [VERIFIED, Bot API 10.3, обновл. 2026-08-24]: `createForumTopic` → `message_thread_id`;
  `sendMessage(…, message_thread_id=…)`; `copyMessage` (без «Forwarded from»).
- Лимиты [VERIFIED, Bot FAQ]: 1 msg/s в чат, **20 msg/min в группу** (на супергруппу целиком, не на топик),
  ~30 msg/s bulk; при превышении — `429 retry_after` → backoff. Под умеренной нагрузкой не ограничивает.

## 8. Адаптер на стороне продукта (тонкий — только чеканка токена)

Весь UI (кнопка + панель + лента + ввод) — в бандле `dali-support` (`dali_support/widget/widget.{js,css}`),
вендорится в статику продукта при сборке, отдаётся как статический ресурс, подключается загрузочным скриптом
продукта (`<link>`+`<script>` с cache-bust `?v=version`). Продукт НЕ держит UI-элемент, server-push, реестр
сессий и роут callback. Остаётся одно:

- **`POST /support/config`** (эндпоинт продукта, идентифицирует залогиненного по своей сессии). ⚠️ Именно
  **POST, не GET**, если фронтенд продукта — SPA с catch-all на GET: такой роут перехватил бы GET и отдал
  index.html (вернулся бы HTML вместо JSON). Идентификация с СЕРВЕРА (из cookie/сессии продукта) → чеканит
  `mint_widget_token(user_ref)` (нормализованный ключ identity, например `strip+lower` логина) →
  `{api_base, token, ttl}`. 401 — не залогинен; 404/инертно — нет `SUPPORT_WIDGET_SECRET`.
- **Токен** — `b64url(user_ref).exp.HMAC_SHA256(SUPPORT_WIDGET_SECRET, '<b64>.<exp>')`; схема 1:1 с
  `dali_support/tokens.py` (relay проверяет). Короткоживущий (`ttl`, дефолт 900с); виджет перезапрашивает
  токен (`POST /support/config`) при ответе `401` — реактивно, отдельного таймера по `ttl` нет.
- **Виджет живёт параллельно** основному интерфейсу продукта, не смешиваясь с ним.
- Доставка ответов оператора — polling виджета к relay (`/v1/widget/messages`), сервер-пуша со стороны
  продукта нет.
- **Обезличивание оператора:** `author` у `out`-строк relay всегда пишет как **«Оператор»** (`OPERATOR_DISPLAY`)
  — реальное имя/username не уходит в ленту/браузер; атрибуция конкретного оператора остаётся в Telegram-топике.

## 8b. Виджет-бандл (`dali_support/widget/`) — ванильный, самодостаточный

- `widget.js`: на старте `POST /support/config`; нет токена → молча не поднимается (self-gating). Рисует
  `#dali-support-fab` (💬, fixed снизу слева) + `.dali-support-drawer` (справа, `translateX` по классу open);
  polling `GET {api_base}/v1/widget/messages?since=<cursor>` (видимая вкладка ~4с, скрытая реже); отправка
  `POST {api_base}/v1/widget/send`; оптимистичное эхо + дедуп по `message_id`; бейдж непрочитанных при
  закрытой панели; восстановление ленты `since=0`; перезапрос токена при `401` (протух → `/support/config`).
- `widget.css`: под `#dali-support-root`, дизайн-токены хоста (`--primary`/`--card`/… с фолбэками) → тёмная
  тема и палитра продукта подхватываются автоматически; на телефоне панель во весь экран.
- **Текст оператора — только `textContent`** (приходит из Telegram, без `innerHTML`).

## 9. Контур и безопасность

- Сообщение в поддержку = произвольный текст пользователя → может нести чувствительное. v1: поддержка
  операционная; на INFO/WARNING логируем **только метаданные** (длины/счётчики/`ticket_id`), не текст.
- **IPv6 egress:** в некоторых сетях/облаках `api.telegram.org` достижим **только по IPv6**, а docker-сеть
  обычно без IPv6. long-polling relay — **долгоживущий** egress (не разовый запрос) → контейнеру relay нужен
  рабочий IPv6 к Telegram, локальный Bot API server, либо forward-proxy (`SUPPORT_TG_PROXY`). Открытый пункт
  развёртывания (§13 Ф2/Ф4).
- Секреты — только `.env`/секрет-менеджер (`SUPPORT_TG_BOT_TOKEN`, `SUPPORT_WIDGET_SECRET` и пр.), не в UI.
- **Браузер-facing поверхность (ревизия 2026-10-16):** relay доступен браузеру ТОЛЬКО через reverse-proxy
  same-origin путь `/support-api/v1/widget/*` (strip-prefix → `dali-support:8500`); same-origin → без CORS.
  Публичен лишь namespace `/v1/widget/*` — ingress `/v1/messages`, health и прочее relay наружу НЕ выводятся.
  Каждый `/v1/widget/*` закрыт Bearer-токеном (`SUPPORT_WIDGET_SECRET`, exp-проверка, `compare_digest`);
  `user_ref` в токене — с сервера (анти-спуфинг), пользователь видит только свою ленту.
- Отход от постуры «relay строго внутренний» — осознанное решение ради переиспользуемого виджета
  (минимальная интеграция). Лёгкая альтернатива — «через продукт» (relay внутренний, прокси+SSE) — не
  выбрана. Токен браузера и product-ингресс HMAC — `hmac.compare_digest`.

## 10. Конфигурация (env)

| Переменная | Где | Назначение |
|---|---|---|
| `SUPPORT_TG_BOT_TOKEN` | relay | токен бота поддержки (@BotFather) |
| `SUPPORT_TG_GROUP_ID` | relay | chat_id forum-супергруппы (бот — админ с «Управление темами») |
| `SUPPORT_TG_PROXY` | relay | forward-proxy egress к Telegram (обход IPv6-блокера, §9) |
| `SUPPORT_WIDGET_SECRET` | relay+продукт | **общий** секрет подписи токена доступа виджета (продукт чеканит, relay проверяет) |
| `SUPPORT_API_BASE` | продукт | базовый адрес widget-API для браузера (прод `/support-api`; dev `http://localhost:8500`) |
| `SUPPORT_WIDGET_TOKEN_TTL` | продукт | TTL токена, с (дефолт 900) |
| `SUPPORT_ALLOWED_ORIGIN` | relay | origin продукта для CORS (локальный кросс-оригин dev; пусто → без CORS, прод same-origin) |
| `SUPPORT_PRODUCT_CALLBACK_URL` | relay | **пусто** в модели «виджет» → callback/outbox дормант; задан → модель «через продукт» |
| `SUPPORT_INGRESS_SECRET` / `SUPPORT_CALLBACK_SECRET` | relay | HMAC продукт→relay / relay→продукт (модель «через продукт») |
| `SUPPORT_DB_URL` | relay | хранилище тикетов+ленты+outbox (дефолт `sqlite:///./dali_support.db`) |
| `SUPPORT_HTTP_PORT` | relay | порт relay (дефолт 8500) |
| `SUPPORT_OUTBOX_RETRY_INTERVAL` / `SUPPORT_OUTBOX_TTL` / `SUPPORT_OUTBOX_MAX_ATTEMPTS` / `SUPPORT_OUTBOX_BACKOFF_BASE` / `SUPPORT_OUTBOX_BACKOFF_CAP` | relay | повтор callback оффлайн-пользователю (контур B): период/TTL/потолок попыток/база и потолок backoff (30/86400/12/30/1800) |
| `SUPPORT_LOG_LEVEL` | relay | INFO/DEBUG; на INFO/WARNING — только метаданные, не текст сообщений |

## 11. Развёртывание

- Co-location: сервис `dali-support` в compose продукта, та же docker-сеть. Образ собирается из этого
  репозитория (`dali-support:local`) либо берётся из реестра контейнеров.
- **Публичный маршрут:** на reverse-proxy продукта — правило `path /support-api/v1/widget/*` → strip-prefix
  `/support-api` → `reverse_proxy dali-support:8500` (same-origin с продуктом; наружу выводится ТОЛЬКО
  widget-namespace, ingress `/v1/messages` не публикуется).
- **Вендоринг бандла:** сборка продукта копирует `dali_support/widget/{widget.js,widget.css}` в свою статику
  (напр. `public/support-widget.*`) → в образ. Источник истины — репозиторий `dali-support`; скопированный
  артефакт в продукте держать gitignored.
- **Секрет `SUPPORT_WIDGET_SECRET`** — общий у продукта и relay; хранится в секрет-менеджере продукта (env),
  в UI/настройках-как-код не светится.

## 12. Тестирование

- **Подпроект (оффлайн):** стор CRUD + HMAC ingress (Ф0/Ф1); **лента `messages` + токен + widget-API**
  (`tests/test_widget_unit.py`): `add_message`/`changes_since` (курсор), round-trip токена (подделка/
  просрочка/пустой секрет), `/v1/widget/{send,messages}` TestClient (401 без/с плохим токеном, форма/порядок,
  чужой токен не видит чужую ленту), дормантность callback без `product_callback_url`. aiogram замокан.
- **Адаптер на стороне продукта (оффлайн):** в репозитории продукта — юниты на нормализацию `user_ref`,
  `enabled`-гейт, схему `mint_widget_token` (сверка с relay-формулой `dali_support/tokens.py`),
  `/support/config` (identity из сессии, 401 аноним, 404/инертно без секрета). Виджет (ванильный JS) — ручной smoke.

## 13. Фазы (коммит на фазу)

- **Ф0 — скелет проекта ✅** (создан): layout, DTO-контракт, стор SQLite, ingress-роут/health, каркас
  тестов, README/docs. Без Telegram-логики. `ruff`/`py_compile` чистые. Отдельный репозиторий.
- **Ф1 — ingress + callback ✅:** `relay.ingress`/`_dispatch` (httpx), HMAC-гейт ingress-роута. Оффлайн-E2E
  продукт→relay с замоканным `TelegramOps` + маршрутизация оператор→callback.
- **Ф2 — Telegram-сторона 🟡** (код готов, нужен smoke): `telegram_ops.start_polling` (aiogram-хэндлеры
  forum-топиков, `/close`|`/ban`), `ensure_topic`/`send_to_topic`. Оффлайн-юниты. **Остаётся:** smoke на
  тестовой супергруппе + **IPv6-egress** (§9).
- **Ф3 — адаптер продукта 🟡** (env-gated, нужен smoke): `/support/push` (HMAC) + реестр `user_ref→{session_id}` +
  server-push в живую сессию. Инертен без адреса relay. Оффлайн-юниты. **Остаётся:** сквозной smoke.
- **Ф3b — фолбэк истёкшей сессии (в релее) ✅:** outbox + повтор callback с backoff до `delivered>0`/TTL.
  Модель B вместо persist+resume в продукте — адаптер остаётся тонким (§8).
- **Ф4 — развёртывание ✅:** сервис `dali-support` в compose продукта, `SUPPORT_*` в секрет-менеджере,
  сборка образа. IPv6-egress решён forward-proxy.
- **Ф5 — встраиваемый виджет (ревизия 2026-10-16) 🟡** (код готов, нужен деплой+smoke): UI вынесен в
  `dali_support/widget/` (ванильный бандл); relay получил `messages`-ленту + token-gated `/v1/widget/*`
  (polling) + `tokens.py`; продукт сведён к `/support/config` (чеканка токена) + вендоринг бандла; снят
  server-push/реестр/`/support/push`. Маршрут `/support-api` на reverse-proxy, секрет `SUPPORT_WIDGET_SECRET`.
  Оффлайн-тесты зелёные (§12). **Остаётся:** деплой→CD + сквозной smoke в браузере + `SUPPORT_WIDGET_SECRET`
  в секрет-менеджере.

## 14. Открытые решения

- **Единый тред vs отдельное окно** — **РЕШЕНО (ревизия 2026-10-16): отдельный виджет** (кнопка 💬 +
  панель справа, своя лента), полностью вне основной ленты продукта. Чище прежнего «режима»: диалог с
  человеком и с ботом/ИИ идут параллельно, их нельзя перепутать.
- **Один бот или отдельный от основного бота продукта:** рекомендуется отдельный бот поддержки + отдельная
  супергруппа (разные роли; алерты/прочее и поддержка не должны мешаться).
- **IPv6 egress контейнера relay** — проверить/настроить под сеть развёртывания (§9).
- **Чувствительность:** если поддержка начнёт касаться конфиденциального содержимого продукта — пересмотреть
  контур.

---
*Документ живой: обновлять при смене контракта/фаз. Это спека проекта `dali-support/` (держать §4–§7 в
синхроне с кодом). Интеграционная сторона (§8/§11) реализуется на стороне продукта.*
