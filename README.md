# dali-support

**Embeddable relay: web‑chat user ⇄ support operator in Telegram.**

A user types from a support widget inside your web product; an operator reads and
replies from a **Telegram forum topic**; the reply flows back into the user's live
session. `dali-support` is a small, self‑contained FastAPI service that knows
**nothing about your product** — only an opaque `user_ref` and an HTTP contract — so
it drops into any web app behind a thin adapter.

[![CI](https://github.com/Neanderthal/dali-support/actions/workflows/ci.yml/badge.svg)](https://github.com/Neanderthal/dali-support/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

> 📖 Detailed design docs (in Russian) live in [`docs/`](docs/README.md): architecture
> notes, the HTTP contract, and the product‑integration guide.

## Why

Off‑the‑shelf live‑chat tools cover the *inverse* flow — the user writes in Telegram
while the operator sits in a vendor web console (Chatwoot, Jivo, Crisp, …). The
"**user in the web, operator in Telegram**" flow is niche. `dali-support` keeps every
conversation inside your own Telegram workspace, reuses infrastructure you already run
(a bot, a supergroup), and sends nothing to third parties.

## Features

- 🧩 **Product‑agnostic** — the relay only sees an opaque `user_ref` and an HTTP
  contract. Zero framework coupling.
- 💬 **Drop‑in widget** — a vanilla‑JS/CSS bundle (`dali_support/widget/`), no React,
  no build step. Embed with one `<script>` + `<link>`.
- 🎫 **1 topic = 1 ticket** — each user gets a dedicated Telegram forum topic; operator
  commands `/close`, `/clear`, `/help`, `/del`.
- 🔁 **Live sync** — operator edits (`edited_message`) and recalls (`/del`) propagate to
  the widget; the message feed is the source of truth and survives offline/restart.
- 🔒 **Token‑gated** — the browser talks to the relay with a short‑lived HMAC token
  minted by your product; `user_ref` is taken server‑side (anti‑spoofing).
- 📡 **Long‑polling, not webhooks** — egress‑only to `api.telegram.org`; no public
  inbound endpoint required.
- 📨 **Durable fallback** — the optional "via‑product" callback model retries delivery
  from an outbox with backoff (for offline users).
- 🗄️ **Self‑contained** — SQLite by default; swap in Postgres behind the same `Store`
  interface.
- ✅ **Tested & linted** — offline unit tests (no network/Telegram needed), ruff‑clean.

## Architecture

```
browser user                                    support operator (Telegram)
  widget 💬 (dali_support/widget/)                     ▲
  │  POST /v1/widget/send            (Bearer token)    │ reply in topic
  │  GET  /v1/widget/messages?since= (polling ~4s)     │
  ▼                                                    │
dali-support  ──createForumTopic / sendMessage──▶  forum supergroup
        ▲ writes `messages` feed ◀── long-polling getUpdates ◀──┘
```

Two contours share one relay:

- **Contour A — browser widget (primary).** The browser talks to the relay directly
  through your reverse proxy (same‑origin `/support-api/v1/widget/*`), authorized by a
  Bearer token. Operator replies are delivered by **polling**.
- **Contour B — via product (optional, config‑gated).** The product posts ingress to
  `/v1/messages` and receives operator replies via a signed HTTP callback with an
  outbox/retry fallback. Dormant unless `SUPPORT_PRODUCT_CALLBACK_URL` is set.

## Quick start

```bash
# with uv (https://docs.astral.sh/uv/)
uv sync
cp .env.example .env         # fill in bot token, group id, SUPPORT_WIDGET_SECRET
uv run uvicorn dali_support.app:app --port 8500
curl localhost:8500/health   # {"status":"ok","service":"dali-support"}
```

With Docker:

```bash
docker compose up -d         # service on :8500 (reads .env)
```

## Configuration

Secrets and settings come from the environment (`.env`) — see
[`.env.example`](.env.example) for the full annotated list.

| Variable | Purpose |
|---|---|
| `SUPPORT_TG_BOT_TOKEN` | support bot token (@BotFather) |
| `SUPPORT_TG_GROUP_ID` | chat_id of the forum supergroup (bot is admin with "Manage topics") |
| `SUPPORT_TG_PROXY` | forward proxy for Telegram egress (empty → direct; useful when Telegram is reachable only over IPv6) |
| `SUPPORT_WIDGET_SECRET` | **contour A:** shared secret the product uses to mint, and the relay to verify, the widget token |
| `SUPPORT_ALLOWED_ORIGIN` | product origin for CORS (local cross‑origin dev; empty → no CORS, same‑origin in prod) |
| `SUPPORT_PRODUCT_CALLBACK_URL` | **contour B:** where to POST operator replies; empty → callback/outbox dormant |
| `SUPPORT_CALLBACK_SECRET` / `SUPPORT_INGRESS_SECRET` | HMAC secrets for the two contour‑B legs |
| `SUPPORT_DB_URL` | storage for tickets + feed + outbox (default `sqlite:///./dali_support.db`) |
| `SUPPORT_HTTP_PORT` | relay port (default 8500) |
| `SUPPORT_OUTBOX_RETRY_INTERVAL` / `_TTL` / `_MAX_ATTEMPTS` / `_BACKOFF_BASE` / `_BACKOFF_CAP` | contour‑B outbox retry tuning |
| `SUPPORT_LOG_LEVEL` | `INFO`/`DEBUG`; at INFO/WARNING only metadata is logged, never message text |

## HTTP API

See [`docs/api.md`](docs/api.md) for the full contract. In short:

- **Contour A** (Bearer token): `POST /v1/widget/send {text}` → `{ok, message_id}`;
  `GET /v1/widget/messages?since=<seq>` → `{messages:[…], cursor}`.
- **Contour B** (HMAC `X-Dali-Signature`): `POST /v1/messages`; signed callback to
  `SUPPORT_PRODUCT_CALLBACK_URL`.
- `GET /health` → `{"status":"ok","service":"dali-support"}`.

## Telegram setup (one‑time)

1. Create a bot via [@BotFather](https://t.me/BotFather) → `SUPPORT_TG_BOT_TOKEN`.
2. Create a **supergroup** and enable **Topics** (forum mode).
3. Add the bot as an administrator with the **Manage topics** right.
4. Take the group's `chat_id` (usually negative, `-100…`) → `SUPPORT_TG_GROUP_ID`.

Operators then work right in the group: one topic per user; reply in the topic;
`/close` closes the ticket, `/clear` wipes both sides, `/help` prints the command list.

## Embedding in a product

Your product does exactly two things (a thin adapter):

1. **Mint a token** for the logged‑in user at `POST /support/config`
   (identity resolved server‑side) using `SUPPORT_WIDGET_SECRET`. The token scheme is
   defined once in [`dali_support/tokens.py`](dali_support/tokens.py).
2. **Vendor the widget bundle** (`dali_support/widget/widget.{js,css}`) into your static
   assets and expose the relay at same‑origin `/support-api/v1/widget/*` via your reverse proxy.

Full integration guide: [`docs/integration.md`](docs/integration.md).

## Storage

SQLite by default (zero infrastructure). For a multi‑user / fault‑tolerant setup,
implement the same `store.Store` interface over Postgres and inject it in
`app.create_app`. Schema and methods: [`dali_support/store.py`](dali_support/store.py).

## Development

```bash
uv sync
uv run ruff check .          # lint
uv run pytest tests/ -q      # offline tests — no network/Telegram needed
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for conventions and the PR workflow.

## Project layout

```
dali_support/
├── config.py        # env → Config (secrets from env only)
├── models.py        # DTOs — the embedding boundary
├── store.py         # Store: tickets ↔ topic ↔ user + messages feed (SQLite)
├── telegram_ops.py  # aiogram: forum topics + long-polling (text/commands/edits)
├── tokens.py        # mint/verify short-lived widget access token (HMAC+exp)
├── relay.py         # orchestration: ingress / operator reply / edit / delete
├── api.py           # FastAPI router: POST /v1/messages (HMAC) + GET /health
├── widget_api.py    # public widget API: /v1/widget/{send,messages} (Bearer)
├── widget/          # embeddable UI bundle: widget.js + widget.css (vanilla)
└── app.py           # app assembly + lifespan (long-polling) + CORS
tests/               # offline unit tests (contract / store / HMAC / widget)
docs/                # detailed design docs (Russian)
```

## Security

Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md). The relay
handles arbitrary user text and bot credentials: keep secrets in the environment only,
and log metadata, not message content.

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and
the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © 2026 Sergey Istomin
