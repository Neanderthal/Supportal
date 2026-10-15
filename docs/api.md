# HTTP-контракт dali-support

Три связки. Все тела подписываются HMAC-SHA256 заголовком `X-Dali-Signature` (сверка `hmac.compare_digest`).

## 1. Продукт → relay: `POST /v1/messages`
Секрет — `SUPPORT_INGRESS_SECRET`.
```json
{
  "user_ref": "engineer@example.com",
  "text": "Нет давления в контуре после замены насоса",
  "display_name": "Иванов · Husky-линия",
  "meta": {"product": "dali-chat", "page": "Husky MM"}
}
```
- `user_ref` — стабильный непрозрачный id пользователя (у ДаЛи — нормализованный логин `strip+lower`). Ключ тикета.
- `meta` — показывается оператору в шапке топика, relay его не интерпретирует.

Ответ `200`:
```json
{"ok": true, "ticket_id": "e3f1…", "thread_id": 45}
```
`401` — неверная/отсутствующая подпись.

## 2. Relay → продукт: callback на `SUPPORT_PRODUCT_CALLBACK_URL`
Секрет — `SUPPORT_CALLBACK_SECRET`. Шлётся при ответе оператора и при закрытии тикета.
```json
{
  "event": "message",
  "user_ref": "engineer@example.com",
  "ticket_id": "e3f1…",
  "text": "Здравствуйте. Проверьте сапун бака и уровень масла…",
  "operator": "support1"
}
```
- `event ∈ {message, ticket_closed}`. У `ticket_closed` поле `text` пустое.
- Адаптер продукта обязан: проверить подпись → по `user_ref` найти живую сессию → доставить сообщение;
  сессии нет → сохранить и отдать при следующем входе (fail-soft, relay доставку не гарантирует).

## 3. Telegram → relay
Внутренний путь: relay сам забирает обновления через **long-polling** (getUpdates) и маршрутизирует ответы
операторов по `message_thread_id`. Отдельного HTTP-эндпоинта у продукта для этого не требуется.

## Здоровье: `GET /health`
```json
{"status": "ok", "service": "dali-support"}
```
