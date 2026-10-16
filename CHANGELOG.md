# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
  The demo ships a sample (Spanish) localization.
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
  env string); `settings.get_dict` added. Operator-side only (no widget surface); the demo ships Spanish presets.

### Changed
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

### Security
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
