# Roadmap

Directional plan for `supportal`. This is **not a commitment** — priorities shift
with feedback and contributions. Have an opinion or want to pick something up? Open an
issue or a discussion (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Guiding principle

`supportal` is a **thin, product‑agnostic relay**, not a live‑chat platform. The key
design choice: **the entire operator/agent experience is offloaded to Telegram**. So a
large class of competitor features (agent console, mobile agent apps, team inbox, search,
assignment, internal notes, agent notifications) are *out of scope by design* — Telegram
already provides them. Compared to general live‑chat tools (Intercom, Crisp, Chatwoot,
Tawk.to, Jivo, …), we deliberately stay small and embeddable.

The roadmap therefore focuses on: the end‑user widget, richness of the message channel,
operator conveniences that can live inside Telegram, product‑integration ergonomics, and
maturity as an open‑source platform.

**Scope tags:** 🧩 fits the thin relay · 🔌 optional pluggable module · ⚙️ infra / platform maturity.

## Current capabilities (v0.1)

- Embeddable vanilla‑JS/CSS widget: host theme tokens, dark mode, mobile full‑screen, unread badge.
- One Telegram forum topic per user; operator commands `/close`, `/clear`, `/help`, `/del`.
- Operator reply, edit‑sync (`edited_message`), and recall (`/del`); operator identity anonymized.
- Persistent message feed (source of truth): polling delivery, offline‑safe, restore on reload.
- Token‑gated browser contour (HMAC), product‑agnostic HTTP contract, anti‑spoofing `user_ref`.
- Optional "via‑product" callback contour with an outbox + backoff retry for offline users.
- SQLite by default; swappable `Store` interface for Postgres.
- Telegram long‑polling (egress‑only, no public inbound webhook). Offline unit tests, ruff‑clean, CI.

## v0.2 — internationalization & a richer channel

- [x] 🧩 **Attachments / images** both directions (`SUPPORT_ATTACHMENTS`, default off): widget upload +
      inline/render, operator photos/documents relayed, bytes in the DB. Retires the largest v1 non‑goal.
- [x] 🧩 **Widget i18n** — UI copy extracted into overridable strings (English default), localized by the
      product via a `strings` object in `/support/config`.
- [x] 🧩 **Visitor context into the topic header** (`SUPPORT_VISITOR_CONTEXT`, default off) — page URL /
      title / custom fields posted once into the topic for the operator.
- [x] 🧩 **Rate limiting / basic anti‑spam** on `POST /v1/widget/send` (`SUPPORT_RATE_LIMIT`, default off),
      per `user_ref` sliding window.
- [ ] ⚙️ **OpenAPI / `/docs`** (free with FastAPI) and a reference product adapter in `examples/`.

## v0.3 — responsiveness & operator convenience

- [ ] 🧩 **SSE transport** alongside polling (lower latency; graduates a v1 non‑goal). Contract preserved.
- [ ] 🧩 **Delivery / read status + typing indicator** (lightweight, over the feed / SSE).
- [x] 🧩 **CSAT / rating** after `/close` (widget buttons → feed row / log) — `SUPPORT_CSAT`, default off.
- [ ] 🔌 **Canned responses** — on the Telegram side (bot commands / inline), no agent console.
- [ ] 🧩 **Proactive message** — `POST /v1/widget/push` so the product can initiate a message to a user.
- [ ] ⚙️ **Reference Postgres `Store`** implementation + migrations.

## v1.0 — platform maturity

- [ ] ⚙️ **Observability**: `/metrics` (Prometheus), structured logs (health endpoint already present).
- [ ] ⚙️ **GDPR operations**: export / delete a user's conversation by `user_ref`, retention policy.
- [ ] ⚙️ **Widget tests** (currently manual smoke) + E2E against a test supergroup in CI.
- [ ] 🧩 **Business hours / auto‑away** message.

## Later / optional modules

Kept as plugins/adapters so the core stays product‑agnostic.

- [ ] 🔌 **AI / LLM auto‑answer** from a knowledge base before handing off to a human.
- [ ] 🔌 **Multi‑channel ingress** (e‑mail / WhatsApp) → the same Telegram‑operator model.
- [ ] 🔌 **Multi‑tenant** (several products / supergroups) + a minimal admin UI and tags.

## Explicitly out of scope

Provided by Telegram or by the embedding product, not rebuilt here:

- Agent console / desktop, mobile agent apps
- Assignment / routing, SLA engine
- A heavy built‑in CRM
