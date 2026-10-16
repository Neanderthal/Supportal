# Security Policy

## Supported versions

`dali-support` is pre‑1.0. Security fixes are applied to the latest `main` and the most
recent tagged release.

| Version | Supported |
|---|---|
| latest `main` / newest release | ✅ |
| older tags | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

- Preferred: use GitHub's **private vulnerability reporting**
  ("Security" tab → "Report a vulnerability").
- Or email **sergey.istomin@gmail.com** with details and, if possible, a minimal
  reproduction.

We aim to acknowledge reports within a few days and will coordinate a fix and
disclosure timeline with you.

## Handling secrets and data

`dali-support` relays arbitrary user text and holds bot credentials. When deploying:

- Keep all secrets (`SUPPORT_TG_BOT_TOKEN`, `SUPPORT_WIDGET_SECRET`,
  `SUPPORT_*_SECRET`) in the environment only — never in code, config‑as‑code, or VCS.
- The widget token is minted server‑side; `user_ref` is derived from the product
  session, never trusted from the browser (anti‑spoofing).
- Expose only the `/v1/widget/*` namespace to the browser (same‑origin, via your
  reverse proxy); keep ingress/health internal.
- Logs contain metadata only (lengths, counters, `ticket_id`), never message text, at
  `INFO`/`WARNING`.
- HMAC comparisons use `hmac.compare_digest` (constant‑time).

A user's support message may contain sensitive content — treat the Telegram
supergroup and the relay's datastore accordingly.
