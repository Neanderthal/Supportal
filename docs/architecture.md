> **Архитектурные заметки `supportal`.** Зачем сервис существует, его границы и модель данных —
> для контрибьюторов. Детали HTTP-контракта — [api.md](api.md); встраивание и настройка Telegram —
> [integration.md](integration.md). **Аудитория: контрибьюторы.** Дата: 2026-10-16.

# Архитектура Supportal

## Зачем свой relay, а не готовый продукт

Вывод разведки рынка (сверено по первоисточникам): рынок закрывает «обратную» схему — пользователь
пишет в Telegram, а оператор сидит в веб-консоли вендора (Chatwoot, Jivo, Crisp, Chaport, Tawk.to,
RU-агрегаторы). Схема «пользователь в вебе, оператор в Telegram» — нишевая: её держат лишь мелкий SaaS
(Human Reply, Chatgram — отдают данные третьей стороне) и отдельные OSS-приложения/виджеты
(LiveHelperChat, Intergram — своё приложение/пузырь). Свой relay кладёт обращения в тот же канал
продукта, переиспользует собственную инфраструктуру (Telegram-бот, при необходимости прокси-мост egress)
и ничего не отдаёт наружу.

## Границы и разделение кода («граница A»)

Relay не зависит от продукта: он знает только непрозрачный `user_ref` и HTTP-контракт. Отсюда разделение:

- **В этом проекте** — всё про Telegram, тикеты, ленту, токен, HTTP-контракт **и весь UI виджета**
  (`supportal/widget/`, ванильный JS/CSS, ноль зависимостей от фреймворков). Виджет встраивается в
  любой веб-продукт одним включением бандла.
- **На стороне продукта** — тонкий адаптер: чеканка токена залогиненному (`POST /support/config`) +
  вендоринг бандла. Продукт даёт только identity; логика и UI остаются снаружи.

**Non-goals (v1):** тикет-очередь/SLA/аналитика, несколько продуктов, вложения/медиа, несколько
операторов на тикет, индикатор «оператор печатает», SSE вместо polling.

## Два контура

- **Контур A — браузерный виджет (основной).** Браузер ходит в relay напрямую (Bearer-токен,
  same-origin через reverse-proxy продукта), ответы оператора забирает polling-ом.
- **Контур B — через продукт (опц., за конфигом).** Продукт шлёт ingress и получает ответы оператора
  подписанным HTTP-callback'ом с outbox-повтором. Дормант без `SUPPORT_PRODUCT_CALLBACK_URL`.

**Long-polling, а не webhook:** relay не нуждается в публичном inbound-эндпоинте — только egress к
`api.telegram.org`. Детали эндпоинтов — [api.md](api.md).

## Модель данных (SQLite по умолчанию)

```sql
tickets(ticket_id PK, user_ref, display_name, thread_id UNIQUE, status 'open|closed', created_at, last_at)
messages(id PK AUTOINCREMENT, ticket_id, user_ref, direction 'in|out', author, text, created_at,
         tg_message_id, deleted, seq)
outbox(id PK AUTOINCREMENT, ticket_id, user_ref, event, text, operator, status 'pending|delivered|dead',
       attempts, created_at, next_attempt_at)
```

- **`tickets`** — один активный топик на пользователя (`get_open_by_user`) + обратный маршрут по
  `thread_id` (`get_by_thread`).
- **`messages`** — источник правды ленты: для polling виджета, восстановления при перезагрузке и
  правок/отзывов. `seq` — монотонный курсор, растущий на insert/edit/delete, поэтому правки и удаления
  старых строк тоже доходят до виджета (`changes_since(user_ref, after_seq)`). Строки персистентны →
  ответ оффлайн-пользователю подтянется при следующем опросе. `tg_message_id` связывает строку с
  сообщением в Telegram для edit-sync (`edited_message`) и `/del`; `deleted` — мягкое удаление.
- **`outbox`** (контур B) — ответы оператора, которые продукт не подтвердил доставленными; фоновый цикл
  повторяет callback с backoff до `delivered>0` или TTL.

Для многопользовательского/отказоустойчивого контура — Postgres за тем же интерфейсом `store.Store`
(подставляется в `app.create_app`); схема и методы — `supportal/store.py`.
