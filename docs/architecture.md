> **`supportal` architecture notes.** Why the service exists, its boundaries and data model —
> for contributors. HTTP-contract details — [api.md](api.md); embedding and Telegram setup —
> [integration.md](integration.md). **Audience: contributors.** Date: 2026-10-16.

# Supportal architecture

## Why our own relay, not an off-the-shelf product

Market-research conclusion (checked against primary sources): the market covers the "reverse" setup — the user
writes in Telegram while the operator sits in a vendor's web console (Chatwoot, Jivo, Crisp, Chaport, Tawk.to,
RU aggregators). The "user on the web, operator in Telegram" setup is niche: only small SaaS
(Human Reply, Chatgram — they hand data to a third party) and a few OSS apps/widgets
(LiveHelperChat, Intergram — their own app/bubble) support it. Our own relay drops tickets into the product's own
channel, reuses our own infrastructure (a Telegram bot, plus an egress proxy bridge if needed)
and hands nothing to the outside.

## Boundaries and code split ("boundary A")

The relay does not depend on the product: it knows only the opaque `user_ref` and the HTTP contract. Hence the split:

- **In this project** — everything about Telegram, tickets, the feed, the token, the HTTP contract **and the entire widget UI**
  (`supportal/widget/`, vanilla JS/CSS, zero framework dependencies). The widget embeds into
  any web product with a single bundle include.
- **On the product side** — a thin adapter: minting a token for the logged-in user (`POST /support/config`) +
  vendoring the bundle. The product provides only identity; the logic and the UI stay outside.

**Non-goals (v1):** ticket queue/SLA/analytics, multiple products, multiple
operators per ticket, an "operator is typing" indicator, SSE instead of polling.

## Two contours

- **Contour A — browser widget (primary).** The browser talks to the relay directly (Bearer token,
  same-origin via the product's reverse-proxy), and picks up operator replies via polling.
- **Contour B — via the product (optional, behind config).** The product sends ingress and receives operator replies
  via a signed HTTP callback with outbox retry. Dormant without `SUPPORT_PRODUCT_CALLBACK_URL`.

**Long-polling, not a webhook:** the relay needs no public inbound endpoint — only egress to
`api.telegram.org`. Endpoint details — [api.md](api.md).

## Data model (SQLite by default)

```sql
tickets(ticket_id PK, user_ref, display_name, thread_id UNIQUE, status 'open|closed', created_at, last_at)
messages(id PK AUTOINCREMENT, ticket_id, user_ref, direction 'in|out', author, text, created_at,
         tg_message_id, deleted, seq, kind 'msg|csat_request|csat|away', read, attachment_id)
attachments(id PK AUTOINCREMENT, user_ref, direction 'in|out', filename, mime, size, data BLOB, created_at)
outbox(id PK AUTOINCREMENT, ticket_id, user_ref, event, text, operator, status 'pending|delivered|dead',
       attempts, created_at, next_attempt_at)
seq_counter(v)   -- single-row monotonic source for messages.seq (atomic UPDATE … RETURNING)
```

- **`tickets`** — one active topic per user (`get_open_by_user`) + the reverse route by
  `thread_id` (`get_by_thread`).
- **`messages`** — the feed's source of truth: for widget polling, recovery on reload, and
  edits/recalls. `seq` is a monotonic cursor that grows on insert/edit/delete, so edits and deletions
  of old rows also reach the widget (`changes_since(user_ref, after_seq)`). Rows are persistent →
  a reply to an offline user is picked up on the next poll. `tg_message_id` links a row to the
  Telegram message for edit-sync (`edited_message`) and `/del`; `deleted` is a soft delete. `kind`
  distinguishes a normal message from the close-notice/CSAT/auto-away rows; `read` backs read-receipts
  (`SUPPORT_RECEIPTS`); `attachment_id` → `attachments.id` when the row carries a file.
- **`attachments`** (`SUPPORT_ATTACHMENTS`) — file bytes stored in the DB (no object store), size-capped
  at upload, referenced by `messages.attachment_id`; served to the widget via `GET /v1/widget/attachment/{id}`.
- **`seq_counter`** — a single-row counter bumped atomically (`UPDATE … RETURNING`) so `messages.seq` is
  race-free even if DB work later moves off the event loop.
- **`outbox`** (contour B) — operator replies the product did not confirm as delivered; a background loop
  retries the callback with backoff until `delivered>0` or TTL.

For larger-corpus / fault-tolerant storage — Postgres behind the same `store.Store` interface
(swapped in `app.create_app`); the schema and methods are in `supportal/store.py`.

## Scaling (single-process invariant)

**supportal runs as exactly one process.** The Telegram long-poller, the outbox-retry loop and the
in-memory rate limiter (`ratelimit.py`) are all per-process singletons. Do **not** run `uvicorn --workers >1`
or multiple replicas:

- Two `getUpdates` consumers → Telegram answers one with **409 Conflict**; operator replies split silently.
- The outbox loop would deliver each retry **twice** (once per process).
- The rate limiter is in-memory, so limits become **per-worker**, not global.

Swapping SQLite for **Postgres does not lift this by itself** — it only makes the *store* shared. Horizontal
scale-out additionally requires electing a **single leader** for the poller + outbox (and a shared rate
limiter, e.g. Redis). Until that exists, treat "one process" as a hard deployment constraint — it is stated
loudly at startup (the `app.create_app` lifespan log) and in the module docstring of `supportal/app.py`.
