# Embedding into a product + Telegram setup

## Product adapter responsibilities
The relay does not depend on the product. The embedding side writes a thin adapter that:
1. **Outbound:** a user's chat message → `POST {RELAY}/v1/messages` with `{user_ref, display_name,
   text, meta}` + the `X-Supportal-Signature` header (HMAC over `SUPPORT_INGRESS_SECRET`).
2. **Inbound:** accept the callback from the relay, verify the HMAC (`SUPPORT_CALLBACK_SECRET`), and by `user_ref`
   deliver `text` into the user's **live** session; no session → store it and hand it over on the next login.
3. **Identity:** `user_ref` is a stable key (a normalized login). One key per user across all
   tabs/sessions, otherwise replies will diverge.

A typical adapter (server-push into the live session, a `user_ref → session_id` registry, a fallback for an expired session)
is written on the specific product's side. The overall split — [architecture.md](architecture.md).

## Widget theme
The widget takes its palette from the host's design tokens in the **shadcn format — HSL channels** `"H S% L%"`
(e.g. `--primary: 221 83% 53%`), because internally it wraps them in `hsl(var(--primary))`.
⚠️ **Not hex and not `hsl(...)`** — `hsl(#2563eb)` is invalid and the bubbles will "break" (become invisible).
No tokens — the widget uses its built-in fallbacks and stays readable. Example — `examples/demo/index.html`.

## Widget localization (i18n)
The widget ships **English** defaults and renders them via `textContent`/`setAttribute` (never `innerHTML`).
The product knows the user's language, so it localizes by returning an optional **`strings`** object in
`POST /support/config`; provided keys override the defaults, the rest stay English:

```json
→ 200 {"api_base":"/support-api","token":"…","ttl":900,
       "strings":{"title":"Soporte","greeting":"Describe tu problema…","placeholder":"Mensaje…","send":"Enviar"}}
```
Keys: `title`, `support` (launcher aria-label), `close`, `greeting`, `placeholder`, `send`, `attach`,
`sendFail`, `uploadFail`, `uploading` (`{name}` is substituted). The only **server**-generated user-facing
line is the ticket-closed notice — localize it with the `SUPPORT_CLOSED_NOTICE` setting (operator replies
come from Telegram in the operator's own language).

## Telegram-side setup (one-time)
1. Create a bot with @BotFather → `SUPPORT_TG_BOT_TOKEN`.
2. Create a support **supergroup** and enable **Topics** (forum topics) in it.
3. Add the bot to the group as an admin with the **"Manage topics"** right (can_manage_topics).
4. Find the group's `chat_id` (usually negative, like `-100…`) → `SUPPORT_TG_GROUP_ID`.
5. Operators work right in the group: each user gets their own topic; a reply is written into the topic; `/close`
   closes the ticket.

## Storage
Default — SQLite (`sqlite:///…`, self-contained). For a multi-user / fault-tolerant setup
implement a class with the `store.Store` interface over Postgres (the same `tickets` table) and swap it
into `app.create_app`. The schema is in `supportal/store.py`.

## Deploying next to the product
- Co-location: the service in the product's compose, on the same docker network. Ingress and callback travel over internal
  names (`http://supportal:8500`, `http://<product>/…/push`). No public routes need to be added.
- Telegram — egress long-polling. ⚠️ In some networks/clouds `api.telegram.org` is reachable **only over
  IPv6**, while a docker network usually has no IPv6. Two paths: (a) enable IPv6 egress for the container; (b) **`SUPPORT_TG_PROXY`** —
  an HTTP CONNECT forward-proxy on an external host with IPv4 egress to Telegram. The proxy should be locked down by the relay's IP +
  auth + only CONNECT:443 to `api.telegram.org`.
