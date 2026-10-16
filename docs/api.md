# Supportal HTTP contract

Two contours: **(A) the browser widget directly** (revision 2026-10-16, primary) and **(B) via the product**
(the earlier server-push model, kept behind config). Contour-A bodies are authorized with a Bearer token; contour-B
bodies with an HMAC-SHA256 `X-Supportal-Signature` header (`hmac.compare_digest`).

## 0. Browser widget → relay (contour A — primary)

Exposed through the product's reverse-proxy (same-origin `/support-api/v1/widget/*` → strip-prefix → relay). Authorization —
`Authorization: Bearer <token>`, where the token is minted by the product (`{user_ref, exp}`, signed with `SUPPORT_WIDGET_SECRET`;
the scheme is in `supportal/tokens.py`). `user_ref` comes from the token (server-side), not from the body → anti-spoofing.

**Send:** `POST /v1/widget/send`
```json
{"text": "The password reset email doesn't arrive"}        →  {"ok": true, "message_id": 12}
```
**Poll:** `GET /v1/widget/messages?since=<seq>`
```json
→ {"messages": [{"id": 12, "direction": "in|out", "author": "Operator", "text": "…", "ts": 1.72e9, "deleted": false, "kind": "msg", "read": false, "attachment_id": 0}], "cursor": 17}
```
- Feed changes with `seq > since`, ascending by `seq` (**new + edited + deleted**).
  `cursor` — the max seq of the response → the client sends it in `?since=` on the next request. `since=0` → the whole feed (restore).
- The widget reconciles by `id`: new → add; already on screen → update text (edit-sync); `deleted:true` → remove the bubble.
- `author` for `out` messages is **always "Operator"** (the operator's real name/username is not revealed to the user).
- `kind ∈ {msg, csat_request, csat, away}` — `csat_request` marks the close notice under which the widget draws a rating prompt
  (feature `SUPPORT_CSAT`); `csat` is a recorded rating; `away` is an out-of-hours auto-reply (feature `SUPPORT_BUSINESS_HOURS`).
  `attachment_id > 0` → fetch bytes from `GET /v1/widget/attachment/{id}` (feature `SUPPORT_ATTACHMENTS`).
- `read: true` on an `out` row — the user has acknowledged seeing it (feature `SUPPORT_RECEIPTS`); only the operator side surfaces it.
- `401` — missing/expired/forged token. The user sees only their own feed (the token is bound to `user_ref`).

**Read receipt:** `POST /v1/widget/read` (feature `SUPPORT_RECEIPTS`, default off)
```json
{"seq": 17}                                        →  {"ok": true, "seen": 2}
```
- The widget reports the max feed `seq` it has displayed (while the panel is open). The relay marks the operator's
  `out` messages up to that `seq` as read and sets a 👀 reaction on each in Telegram so the operator learns the user saw the reply.
- `seen` — how many operator messages were newly marked read. Feature off → `{"ok": true, "seen": 0}` (no-op). Idempotent.
- The product should advertise `receipts: true` in `POST /support/config` for the widget to report reads + show delivered ticks.

**CSAT rating:** `POST /v1/widget/csat` (feature `SUPPORT_CSAT`, default off)
```json
{"score": 5, "comment": "fast and helpful"}        →  {"ok": true, "message_id": 31}
```
- Sent after the operator closes the ticket (the widget shows a 1..5 star prompt under the `csat_request` notice).
- The rating is recorded as a `kind:"csat"` feed row and posted into the ticket's Telegram topic so the operator sees it.
- `score` is required and validated to `1..5` (`422` otherwise); `comment` is optional. `404` — feature off or no ticket to rate; `429` — rate-limited.
- The product must also advertise `csat: true` in `POST /support/config` for the widget to render the prompt.

**Operator commands/edits — via Telegram, NOT via HTTP** (the Bot API does not send bots deletion events):
- the operator **edits** their message in the topic → the relay catches `edited_message` → the text is updated in the feed
  (`seq` grows) → the widget updates the bubble;
- **`/del`** as a reply to their own message → the relay marks the row `deleted` → the widget removes the bubble;
- **`/clear`** → the user's entire feed is marked `deleted` + the topic is deleted (`deleteForumTopic`) + the ticket is
  reset → the widget clears, and the next message creates a fresh topic;
- **`/help`** → the bot prints a command hint into the topic (it does not go to the user's feed);
- **`/c <key>`** → send a canned reply (feature `SUPPORT_CANNED`); `/c` with no key lists the configured keys in the topic;
- **`/close`** → close the ticket (with `SUPPORT_CSAT` on, the user is then shown a rating prompt — see `POST /v1/widget/csat`).

## 1. Product → relay: `POST /v1/messages` (contour B)
Secret — `SUPPORT_INGRESS_SECRET`.
```json
{
  "user_ref": "user@example.com",
  "text": "The password reset email doesn't arrive",
  "display_name": "Ivanov",
  "meta": {"product": "web", "page": "Settings"}
}
```
- `user_ref` — a stable opaque user id (e.g. a normalized login `strip+lower`). The ticket key.
- `meta` — shown to the operator in the topic header; the relay does not interpret it.

Response `200`:
```json
{"ok": true, "ticket_id": "e3f1…", "thread_id": 45, "message_id": 12}
```
`401` — invalid/missing signature.

## 2. Relay → product: callback to `SUPPORT_PRODUCT_CALLBACK_URL` (contour B — optional)
Secret — `SUPPORT_CALLBACK_SECRET`. Sent on an operator reply and on ticket closure, **only if**
`SUPPORT_PRODUCT_CALLBACK_URL` is set. In contour A (widget) it is not set → callback/outbox dormant (replies are picked up
by widget polling from §0; the `messages` feed is the source of truth).
```json
{
  "event": "message",
  "user_ref": "user@example.com",
  "ticket_id": "e3f1…",
  "text": "Hello. Please check the \"Spam\" folder — the email may have landed there…",
  "operator": "support1"
}
```
- `event ∈ {message, ticket_closed}`. For `ticket_closed` the `text` field is empty (the relay fills in a default).
- The product adapter must: verify the signature → find live sessions by `user_ref` → deliver the message.

**Product response (important for re-delivery):**
```json
{"ok": true, "delivered": 2}
```
- `delivered` — the number of live sessions the message was actually delivered to.
- `delivered = 0` → the recipient is offline: the relay puts the reply into the **outbox** and retries this callback with backoff
  (`SUPPORT_OUTBOX_*`) until the product returns `delivered > 0` or the TTL expires (then — "give up", WARNING).
  When the user comes back → the adapter registers the session → the next retry gets through.
- The `delivered` field is **absent** from the response → the relay treats it as delivered (compatibility with a simple product that
  does not track delivery — no endless retries).
- "At-least-once" semantics: on a failure after an actual delivery a retry is possible (tolerable for support).

**Boundary A:** the relay retries the HTTP callback relying only on the `delivered` count; it knows nothing about the product's
sessions and server-push — that is entirely the concern of the thin product adapter.

## 3. Telegram → relay
Internal path: the relay itself pulls updates via **long-polling** (getUpdates) and routes operator replies by
`message_thread_id`. No separate HTTP endpoint on the product is required for this.

## Health: `GET /health`
```json
{"status": "ok", "service": "supportal"}
```

## Interactive docs (feature `SUPPORT_OPENAPI`, default off)
With `SUPPORT_OPENAPI=on`, FastAPI serves the Swagger UI at `GET /docs`, ReDoc at `GET /redoc` and the schema
at `GET /openapi.json` (routes are grouped by the `widget` / `product` / `health` tags). Off by default — the
relay usually sits behind the product's reverse-proxy and the API surface need not be public. The flag is read
**at startup** (the docs URLs are fixed when the app is built), so change it and restart.
