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

**Non-goals (v1):** ticket queue/SLA/analytics, multiple products, attachments/media, multiple
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
         tg_message_id, deleted, seq)
outbox(id PK AUTOINCREMENT, ticket_id, user_ref, event, text, operator, status 'pending|delivered|dead',
       attempts, created_at, next_attempt_at)
```

- **`tickets`** — one active topic per user (`get_open_by_user`) + the reverse route by
  `thread_id` (`get_by_thread`).
- **`messages`** — the feed's source of truth: for widget polling, recovery on reload, and
  edits/recalls. `seq` is a monotonic cursor that grows on insert/edit/delete, so edits and deletions
  of old rows also reach the widget (`changes_since(user_ref, after_seq)`). Rows are persistent →
  a reply to an offline user is picked up on the next poll. `tg_message_id` links a row to the
  Telegram message for edit-sync (`edited_message`) and `/del`; `deleted` is a soft delete.
- **`outbox`** (contour B) — operator replies the product did not confirm as delivered; a background loop
  retries the callback with backoff until `delivered>0` or TTL.

For a multi-user / fault-tolerant setup — Postgres behind the same `store.Store` interface
(swapped in `app.create_app`); the schema and methods are in `supportal/store.py`.
