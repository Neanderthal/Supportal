# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Widget color schemes: a `theme` field in `/support/config` selects one of 7 built-in palettes —
  `blue`, `violet`, `emerald`, `amber`, `rose`, `teal` (accent on a light surface) and `dark` (full dark
  surface). A scheme overrides the host's design tokens (sets the widget's `--ds-*` variables); omitting
  `theme` keeps inheriting host tokens, and an unknown value is ignored. The demo advertises `"theme":"violet"`.
- Widget launcher placement: a `position` field in `/support/config` (`"left"` default | `"right"`) pins the
  floating launcher to either bottom corner, and the launcher now auto-hides while the chat panel is open
  (so a right-aligned launcher never overlaps the drawer). The demo advertises `"position":"right"`.
- README glow-up: an SVG project logo + a colorful ASCII terminal banner (`assets/logo.svg`,
  `assets/banner.svg`), demo screenshots of both sides of a conversation (`assets/screenshot-*.png`),
  and a reworked, more developer-oriented layout (feature table, ASCII flow diagram, copy-paste curl
  examples, collapsible project tree).
- Russian localization (`SUPPORT_LOCALE`, default `en`): a built-in `ru` locale pack. Server-generated copy
  — the closed / away notices and the operator / assistant author labels — follows the active locale
  (`supportal/i18n.py`); each notice stays overridable by its own setting (`SUPPORT_CLOSED_NOTICE` /
  `SUPPORT_AWAY_MESSAGE`). The widget ships built-in `en`/`ru` tables, selected by a `locale` field in
  `/support/config` (a `strings` map still overrides individual keys). Unknown locale → English (fail-soft).
  The demo now runs in Russian (built-in pack, Russian canned replies + KB docs).
- Open‑source project scaffolding: MIT `LICENSE`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, this changelog, GitHub issue/PR templates,
  CI and Docker‑publish workflows, Dependabot, `.dockerignore`, `.editorconfig`,
  pre‑commit config.
- English `README.md` and package metadata (authors, license, URLs, classifiers).
- `ROADMAP.md`; replaced the internal mini‑spec with a focused `docs/architecture.md`.
- `examples/demo/` — a local demo page with the embedded widget and a mocked Telegram
  side (mock operator console); documented the widget theming token format.
- Renamed the project `dali-support` → `supportal`.
- Feature-flag foundation: `settings.yaml` (hot-reloaded) → env → default, via
  `supportal/settings.py`. All optional features are off by default and gated by a flag.
- Rate limiting on `POST /v1/widget/send` (`SUPPORT_RATE_LIMIT`, default off) — a
  per-`user_ref` sliding window (`supportal/ratelimit.py`).
- File attachments both ways (`SUPPORT_ATTACHMENTS`, default off): widget upload
  (`POST /v1/widget/upload`) + inline image / download-link rendering; operator photos and
  documents from the topic are downloaded and relayed to the widget; bytes stored in the DB
  (`attachments` table), size-capped by `SUPPORT_ATTACHMENTS_MAX_MB`. The demo shows both directions.
- Widget i18n: UI copy is extracted into overridable strings (English defaults), localized by the
  product via a `strings` object in `POST /support/config` (applied via textContent/setAttribute, not
  innerHTML). The server-generated ticket-closed notice is now the `SUPPORT_CLOSED_NOTICE` setting.
  The demo ships a Russian localization (now via the built-in `ru` locale pack — see above).
- Visitor context (`SUPPORT_VISITOR_CONTEXT`, default off): the widget sends page context (URL/title/
  referrer + custom fields from `window.supportalContext`) and the relay posts a one-time "🧭 Visitor
  context" block into a new topic for the operator (capped/sanitized, display-only). The demo shows it.
- CSAT rating (`SUPPORT_CSAT`, default off): after the operator closes a ticket, the widget shows a 1..5
  star prompt under the close notice; the rating is recorded as a `kind:"csat"` feed row and posted into the
  ticket's topic (`POST /v1/widget/csat`). The `messages` feed grows a `kind` column
  (`msg|csat_request|csat`). The product advertises `csat:true` in `/support/config`; the demo shows it.
- Business hours / auto-away (`SUPPORT_BUSINESS_HOURS`, default off): a ticket opened outside the
  `SUPPORT_HOURS` schedule (`;`-separated `<days> HH:MM-HH:MM`, `SUPPORT_HOURS_TZ` timezone) gets a one-time
  `SUPPORT_AWAY_MESSAGE` auto-reply — an `out` `kind:"away"` feed row plus a `⏰ (auto-away)` note in the
  topic. Schedule parsing is fail-open; the demo is rigged after-hours to show it.
- Canned responses (`SUPPORT_CANNED`, default off): the operator types `/c <key>` in the topic to send a
  preset reply (`/c` alone lists the keys). Presets are `SUPPORT_CANNED_REPLIES` (a YAML map, or a `k=v;k=v`
  env string); `settings.get_dict` added. Operator-side only (no widget surface); the demo ships Russian presets.
- Read receipts (`SUPPORT_RECEIPTS`, default off): the widget reports what it has seen (`POST /v1/widget/read`);
  the relay marks the operator's `out` messages read and sets a 👀 reaction on each in Telegram, and the widget
  shows a `✓` delivered tick. The `messages` feed grows a `read` flag (`store.mark_read`, `TelegramOps.react`).
  Needs `receipts:true` in `/support/config`; the demo shows it (mock console renders `✓✓ seen`).
- OpenAPI docs (`SUPPORT_OPENAPI`, default off): expose Swagger `/docs`, ReDoc `/redoc` and `/openapi.json`,
  with routes tagged `widget` / `product` / `health` (summaries + response models). Read at startup.
- Reference product adapter (`examples/product_adapter.py`): a runnable contour-B example — signs messages to
  the ingress and verifies the operator-reply callback, reporting `delivered` for outbox retry.
- GDPR export / erase (`SUPPORT_GDPR`, default off): `POST /v1/admin/export|delete {user_ref}` (HMAC, contour B)
  export a user's tickets/messages/attachments (base64) or hard-delete them + their Telegram topics
  (`store.export_user`/`delete_user`/`thread_ids_for_user`). The demo page has Export/Delete buttons.
- Observability (`SUPPORT_METRICS`, default off): `GET /metrics` in Prometheus text format — gauges for tickets
  (by status), messages (by direction), attachments, pending outbox, and `supportal_build_info`
  (`store.metrics` + a dependency-free `supportal/metrics.py` renderer). Unauthenticated — internal scrape only.
- AI auto-answer (`SUPPORT_AI`, default off): a minimal KB auto-answer (not RAG) — short `.md` docs from
  `SUPPORT_AI_KB_DIR` are stuffed into an OpenAI-compatible model's context (`SUPPORT_AI_API_BASE/_KEY/_MODEL`);
  it answers from them (a background 🤖 `kind:"ai"` feed row + topic note) or replies `HANDOFF` for a human
  (`supportal/ai.py`; fail-soft). The demo ships two Russian KB docs and stubs the LLM offline.
- SSE transport (`SUPPORT_SSE`, default off): `GET /v1/widget/stream` pushes the same `WidgetPoll` payloads over
  Server-Sent Events (one long-lived connection, `SUPPORT_SSE_INTERVAL` server-side detection) → lower latency
  than polling. The widget uses `EventSource` (token via query) when `sse:true` and falls back to polling. Contract preserved.
- Reference PostgreSQL backend (`supportal/store_pg.py`): `SUPPORT_DB_URL=postgresql://…` selects it via
  `store.make_store` (same `Store` contract as SQLite — seq cursor, edit/delete/clear, read receipts,
  attachments, outbox, GDPR, metrics). Optional extra `supportal[postgres]` (psycopg 3); schema created
  idempotently on startup. Covered by a live integration test (`tests/test_postgres_store_unit.py`, gated on
  `SUPPORT_TEST_PG_URL`).

### Changed
- Docs brought in line with the shipped code (code-review follow-up): documented `POST /v1/widget/upload`
  and the attachment download in `docs/api.md`; refreshed the `docs/architecture.md` data model (the
  `messages` `kind`/`read`/`attachment_id` columns, the `attachments` and `seq_counter` tables) and dropped
  attachments from the v1 non-goals; clarified in `docs/integration.md` that CSAT "rate once" is
  widget-side only (the relay does not deduplicate).
- Smaller architecture-review cleanups (#8): added `supportal/flags.py` — feature-flag/setting **names** as
  constants (`Flags.ATTACHMENTS`, …) so a typo is an `AttributeError`, not a silent fall-back to the default;
  adopted them in `relay.py`/`widget_api.py`. Aligned the `meta` docstring/`docs/api.md` with reality
  (`product`/`page` ARE surfaced in the topic header; other keys are carried, not interpreted). Documented why
  ingress records the feed row **after** the topic send, and that `config.py` (not the `Relay.__init__`
  defaults) is the source of truth for the outbox numbers.
- Refactored the operator-callback boundary (architecture review #7): `TelegramOps.start_polling` /
  `_register_handlers` now take a single `OperatorHandler` object and emit **structured events**
  (`OperatorReply`/`OperatorMediaReply`/`OperatorCommand`/`OperatorEdit`/`OperatorDelete`) instead of five
  positional callbacks (incl. a 7-tuple media alias). `Relay` implements the protocol; the `on_operator_*`
  domain methods are unchanged. Internal only — no change to the HTTP contract or widget.
- Documented the **single-process invariant** (architecture review #3): the Telegram long-poller, the
  outbox-retry loop and the in-memory rate limiter are per-process singletons, so running
  `uvicorn --workers >1` or multiple replicas breaks delivery (409 Conflict, double outbox, per-worker
  rate limits). The constraint is now stated loudly at startup (`app.create_app` lifespan log) and in
  `README.md`, `docs/architecture.md` (§Scaling) and `docs/integration.md`; corrected the "swap in
  Postgres for multi-user" claim (Postgres shares the store only — scale-out also needs a single leader).
- Translated the whole project to English: the `docs/` design docs, all in‑code comments,
  docstrings and log messages, the demo pages, and the default widget UI copy (button
  labels, greeting, placeholder, error notice) and the operator "ticket closed" notice.
  Updated the `CONTRIBUTING.md` docs‑language convention to all‑English accordingly.

### Fixed
- `Store` now has a `close_connection()` lifecycle method, called from the app lifespan on shutdown
  (`supportal/store.py`, `supportal/app.py`) — the SQLite connection was never released, so a long-running
  process that recreated stores leaked connections (and tests showed `ResourceWarning: unclosed database`).
  (Named `close_connection` to avoid colliding with the existing `close(ticket_id)`, which closes a ticket.)
- Business-hours overnight windows now cover the early-morning tail correctly (`supportal/hours.py`): for a
  single-day window like `fri 22:00-02:00`, Saturday 01:00 was wrongly treated as closed because Saturday
  isn't in the schedule. The after-midnight tail is now attributed to the start day.
- Widget polling no longer strands rows from a large `/clear` batch (`supportal/store.py`): `/clear` stamps
  the whole feed with one `seq`, and the client cursor is the page's max `seq`, so a feed larger than the
  200-row page limit left the remaining same-`seq` rows unfetched (stale bubbles surviving a clear).
  `changes_since` now extends the page to the full final-`seq` group so a page always ends on a `seq` boundary.
- `POST /v1/widget/read` now honors `SUPPORT_RATE_LIMIT` like the other write routes
  (`supportal/widget_api.py`): a valid token could otherwise spam read-reports, amplifying into DB writes
  and one Telegram reaction per newly-read message. (The off path was already a cheap no-op — `mark_read`
  returns early before any DB access.)
- Bounded the free-text request fields (`supportal/models.py`, `supportal/widget_api.py`): `text` is capped
  at 4096 chars (Telegram's message limit) on both contours and the CSAT `comment` at 500, declared at the
  DTO boundary → a clean **422** instead of unbounded input or a failed Telegram send.
- Concurrent first-contact from one user no longer creates duplicate Telegram topics
  (`supportal/relay.py`): find-or-create of a ticket+topic ran a check-then-act across `await ensure_topic`,
  so two simultaneous first messages (two tabs / retry / send+upload) both created a topic. Serialized per
  `user_ref` with an `asyncio.Lock` via a shared `_ensure_ticket` helper (used by ingress and attachments).
- Ingress now returns **422** (not an unhandled 500) on a validly-signed but malformed/incomplete `POST
  /v1/messages` body (`supportal/api.py`): the raw body bypasses FastAPI's own validation, so a bad product
  adapter used to trigger a `ValidationError` → 500. Caught → `HTTPException(422)`.
- Made the `messages.seq` change-cursor **atomic** and hardened the SQLite connection (architecture review
  #1/#2): `seq` is now bumped in one statement via a single-row `seq_counter` (`UPDATE … RETURNING`),
  replacing the `SELECT MAX(seq)+1`-then-insert that could hand two writers the same value (silently
  skipping a widget poll cursor) the moment DB work moved off the event loop. The connection runs in **WAL**
  mode with a `busy_timeout`, and the misleading "single writer → no races" comment is corrected. Existing
  DBs seed the counter from their current max seq on startup, so poll cursors stay valid (`supportal/store.py`).
- Operator media that can't be fetched no longer vanishes silently (architecture review #6): a failed
  `getFile`/download (commonly a file above the Bot API's 20 MB limit) is now logged and the operator is
  told in the topic that the user received nothing (`MEDIA_FETCH_FAILED`), instead of dropping the file
  with no signal (`supportal/telegram_ops.py`).

### Security
- Hardened attachment serving (defense-in-depth, code-review follow-up): `GET /v1/widget/attachment/{id}`
  now always sends `X-Content-Type-Options: nosniff` and serves only known raster types
  (png/jpeg/gif/webp) inline — anything else, notably `image/svg+xml`, is forced to download. The endpoint
  is already Bearer-header-gated (not navigable), so this is layered hardening, not a fixed vulnerability.
- Enforce the attachment upload size cap **before buffering** (architecture review #4): `POST
  /v1/widget/upload` used to read the whole body into memory before the `SUPPORT_ATTACHMENTS_MAX_MB` check,
  so an authenticated user could OOM the process. It now rejects on an oversized `Content-Length` up front
  and streams the body with a hard cap (a missing/understated length can't get past it).
- Sanitize the attachment download `Content-Disposition` filename (architecture review #5): the name comes
  from the browser (`?name=`) or Telegram (`document.file_name`), so a `"` or CR/LF could break or inject
  the header. The ASCII `filename="…"` fallback now strips unsafe chars and an RFC 5987 `filename*=UTF-8''…`
  carries the real name safely (`supportal/widget_api.py`).

## [0.1.0] - 2026-10-16

Initial public release.

### Added
- **Contour A — browser widget (primary):** token‑gated `/v1/widget/{send,messages}`
  (polling), short‑lived HMAC widget token (`tokens.py`), persistent `messages` feed
  with edit‑sync / `/del` / `/clear`, and a vanilla‑JS embeddable bundle
  (`supportal/widget/`).
- **Contour B — via product (config‑gated):** ingress `POST /v1/messages` (HMAC),
  signed operator‑reply callback, and an outbox with backoff retry for offline users.
- **Telegram side:** forum‑topic relay over long‑polling (one topic per ticket),
  operator commands `/close`, `/clear`, `/help`, `/del`, and `edited_message` sync.
- SQLite storage by default with a `Store` interface that can be backed by Postgres.
- Offline unit test suite and ruff linting.

[Unreleased]: https://github.com/Neanderthal/supportal/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Neanderthal/supportal/releases/tag/v0.1.0
