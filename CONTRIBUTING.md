# Contributing to Supportal

Thanks for your interest in improving `supportal`! This guide covers the dev setup,
conventions, and the pull‑request workflow.

## Development setup

The project uses [uv](https://docs.astral.sh/uv/) and targets **Python 3.12+**.

```bash
uv sync                                       # create venv + install deps (incl. dev)
cp .env.example .env                          # fill in secrets for a real run
uv run uvicorn supportal.app:app --port 8500
```

## Tests and linting

```bash
uv run ruff check .          # lint (must be clean)
uv run pytest tests/ -q      # offline unit tests — no network or Telegram required
uv run pytest tests/ --cov=supportal --cov-report=term-missing   # with the coverage gate
```

- The test suite is **fully offline**: `aiogram`, HTTP, and the DB are mocked, so tests
  run anywhere without a bot token or a live supergroup.
- Please add or update tests for any behavior change and keep the suite green.
- **Coverage gate:** CI runs the suite with `--cov` and fails below **85%** line coverage
  (`[tool.coverage.report] fail_under` in `pyproject.toml`). Run the `--cov` command above
  locally before a PR; a plain `pytest -q` run stays coverage‑free and fast.

Optionally, install the pre‑commit hooks (runs ruff on commit):

```bash
uvx pre-commit install
```

## Conventions

- **Lint:** ruff with a pinned rule set (`[tool.ruff.lint] select`). The code uses a
  deliberately compact style (`if x: y`, `a; b`, `import a, b`) — `E701/E702/E401` are
  intentionally ignored. Do **not** run `ruff format` (it would expand that style).
- **Logging:** at `INFO`/`WARNING` log **metadata only** (lengths, counters, `ticket_id`),
  **never** user message text. Full text goes to `DEBUG` only. Never swallow exceptions
  silently — log or re‑raise.
- **Secrets:** configuration comes from the environment (`.env`) only. Never hard‑code
  tokens or secrets, and never commit a real `.env` or a `*.db` file.
- **Lazy imports:** `aiogram` is imported lazily (inside `TelegramOps`) so the package
  stays importable (store/models/contract) without it. Keep it that way.
- **Boundary:** the relay must not depend on any specific product — it only knows
  `user_ref` and the HTTP contract. Keep product‑specific logic out of this repo.
- **Docs language:** all documentation — README, community files, the design docs under
  `docs/`, and in‑code comments — is in English. Keep each document in one language;
  don't mix.

## Commit & PR workflow

- Branch off `main`; keep one logical change per commit with a clear message.
- Before opening a PR, make sure `ruff check .` and `pytest tests/ -q` pass.
- Describe the motivation and the change in the PR; link any related issue.
- Update `CHANGELOG.md` (the `Unreleased` section) for user‑visible changes.

## Reporting bugs / proposing features

Use the GitHub issue templates. For security‑sensitive reports, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.
