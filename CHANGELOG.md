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
