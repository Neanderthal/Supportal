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
`sendFail`, `uploadFail`, `uploading` (`{name}` is substituted), `csatPrompt`, `csatThanks`. The only
**server**-generated user-facing line is the ticket-closed notice — localize it with the
`SUPPORT_CLOSED_NOTICE` setting (operator replies come from Telegram in the operator's own language).

## Visitor context (feature `SUPPORT_VISITOR_CONTEXT`, default off)
When on, the widget sends page context with the user's message and the relay posts a **one-time**
"🧭 Visitor context" block into the topic as it is created (operator-facing; not shown in the user's feed).
The widget always includes `url`, `title`, and `referrer`; the host can add **custom fields** by setting a
global before the bundle loads:

```html
<script>window.supportalContext = { plan: "Pro", account: "acme-42" };</script>
<script src="/support-widget.js"></script>
```

`/support/config` should report `"context": true` so the widget collects context only when the relay has
the feature on. The values are display-only — the relay caps field count/length and strips newlines.

## CSAT rating (feature `SUPPORT_CSAT`, default off)
When on, closing a ticket (`/close`) tags the close notice as a `csat_request`; the widget draws a 1..5 star
prompt under it. The user's pick is `POST`ed to `/v1/widget/csat`, recorded as a `kind:"csat"` feed row (so it
survives a reload), and posted into the ticket's Telegram topic (`📊 CSAT: …`) so the operator sees the score.

`/support/config` should report `"csat": true` so the widget renders the prompt only when the relay has the
feature on (mirrors `context`/`attachments`). Localize the prompt/thanks copy with the `csatPrompt`/`csatThanks`
`strings` keys. Each closed conversation can be rated once; a newer close re-arms the prompt.

## Business hours / auto-away (feature `SUPPORT_BUSINESS_HOURS`, default off)
When on, a ticket opened **outside** the schedule gets a one-time "we're away" auto-reply: an `out` message
(`kind:"away"`) in the user's feed plus a `⏰ (auto-away)` note in the topic so the operator knows it was sent.
It fires once, on first contact (topic creation) — it does not nag on every message.

- `SUPPORT_HOURS` — the schedule. `;`-separated `<days> HH:MM-HH:MM` clauses; `<days>` is a range (`mon-fri`),
  a single day (`sat`) or a comma list (`mon,wed,fri`); `end < start` crosses midnight. **Required** — an empty
  or unparseable schedule is fail-open (nobody is ever "away", the feature stays inert).
- `SUPPORT_HOURS_TZ` — IANA timezone the schedule is evaluated in (default `UTC`).
- `SUPPORT_AWAY_MESSAGE` — the reply text (server-generated, so localize it here, like `SUPPORT_CLOSED_NOTICE`).

No `/support/config` flag is needed (this is purely server-side); the widget renders the away message like any
operator reply, with a muted "clock accent" style.

## Canned responses (feature `SUPPORT_CANNED`, default off)
Operator-side quick replies, offloaded to Telegram (no agent console). The operator types **`/c <key>`** in the
topic; the relay expands it to the matching preset, sends it to the user (a normal `out` reply) and posts a
`↩️ (canned: <key>)` confirmation into the topic. `/c` with no key (or an unknown one) lists the available keys
in the topic. Nothing is exposed to the browser widget — this is purely an operator convenience.

- `SUPPORT_CANNED_REPLIES` — the `key → text` map. In `settings.yaml` write it as a nested mapping; via the
  environment use a `key=text;key=text` string. Keys are what the operator types after `/c`.

Because presets live in `settings.yaml`, they can be edited live (no restart), like the other settings.

## Read receipts (feature `SUPPORT_RECEIPTS`, default off)
While the widget panel is open it reports the feed position it has displayed (`POST /v1/widget/read {seq}`).
The relay marks the operator's `out` messages up to that `seq` as read and sets a **👀 reaction** on each one in
Telegram, so the operator learns the user saw the reply. The user's own messages show a `✓` **delivered** tick.

- `/support/config` should report `"receipts": true` so the widget reports reads and renders the delivered tick.
- The 👀 reaction is best-effort (the bot needs reaction permission in the group); a failure is logged, not fatal.
- Read state is operator-facing only — it rides the feed row's `read` flag and does **not** bump the poll `seq`
  (so it never re-notifies the widget). In the demo the mock operator console shows a `✓✓ seen` marker.

## Interactive API docs (feature `SUPPORT_OPENAPI`, default off)
`SUPPORT_OPENAPI=on` exposes the Swagger UI (`/docs`), ReDoc (`/redoc`) and the OpenAPI schema (`/openapi.json`),
with routes grouped by the `widget` / `product` / `health` tags. Off by default (don't publish the API surface
from behind the reverse-proxy unless you mean to). Read at **startup** — change it, then restart.

## Contour B — the reference product adapter
If the product delivers operator replies itself (instead of the browser widget polling), it implements the two
sides of contour B: sign messages to `POST /v1/messages` and verify the signed callback to
`SUPPORT_PRODUCT_CALLBACK_URL`, reporting `delivered` so the relay's outbox retries for offline users. A
runnable, framework-agnostic illustration lives in [`examples/product_adapter.py`](../examples/product_adapter.py).

## Telegram-side setup (one-time)
1. Create a bot with @BotFather → `SUPPORT_TG_BOT_TOKEN`.
2. Create a support **supergroup** and enable **Topics** (forum topics) in it.
3. Add the bot to the group as an admin with the **"Manage topics"** right (can_manage_topics).
4. Find the group's `chat_id` (usually negative, like `-100…`) → `SUPPORT_TG_GROUP_ID`.
5. Operators work right in the group: each user gets their own topic; a reply is written into the topic; `/close`
   closes the ticket.

## Storage
Default — SQLite (`sqlite:///…`, self-contained). For larger-corpus / fault-tolerant **storage**
implement a class with the `store.Store` interface over Postgres (the same `tickets` table) and swap it
into `app.create_app`. The schema is in `supportal/store.py`. ⚠️ Postgres makes the *store* shared but does
**not** by itself allow running more than one process — see the scaling note below.

## Scaling — run exactly one process
supportal is a **single-process service**. The Telegram long-poller, the outbox-retry loop and the in-memory
rate limiter are per-process singletons, so do **not** run `uvicorn --workers >1` or multiple replicas: two
processes mean two `getUpdates` consumers (Telegram returns **409 Conflict** and replies split silently),
double outbox delivery, and per-worker rate limits. Swapping in Postgres only shares the store — horizontal
scale-out additionally needs a **single leader** for the poller + outbox (and a shared rate limiter, e.g.
Redis). The constraint is logged loudly at startup. For throughput, scale **up** (one bigger process), not out.

## Deploying next to the product
- Co-location: the service in the product's compose, on the same docker network. Ingress and callback travel over internal
  names (`http://supportal:8500`, `http://<product>/…/push`). No public routes need to be added.
- **One process only** (see §Scaling): run a single container with a single uvicorn worker.
- Telegram — egress long-polling. ⚠️ In some networks/clouds `api.telegram.org` is reachable **only over
  IPv6**, while a docker network usually has no IPv6. Two paths: (a) enable IPv6 egress for the container; (b) **`SUPPORT_TG_PROXY`** —
  an HTTP CONNECT forward-proxy on an external host with IPv4 egress to Telegram. The proxy should be locked down by the relay's IP +
  auth + only CONNECT:443 to `api.telegram.org`.
