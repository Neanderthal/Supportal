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
→ {"messages": [{"id": 12, "direction": "in|out", "author": "Operator", "text": "…", "ts": 1.72e9, "deleted": false}], "cursor": 17}
```
- Feed changes with `seq > since`, ascending by `seq` (**new + edited + deleted**).
  `cursor` — the max seq of the response → the client sends it in `?since=` on the next request. `since=0` → the whole feed (restore).
- The widget reconciles by `id`: new → add; already on screen → update text (edit-sync); `deleted:true` → remove the bubble.
- `author` for `out` messages is **always "Operator"** (the operator's real name/username is not revealed to the user).
- `401` — missing/expired/forged token. The user sees only their own feed (the token is bound to `user_ref`).

**Operator commands/edits — via Telegram, NOT via HTTP** (the Bot API does not send bots deletion events):
- the operator **edits** their message in the topic → the relay catches `edited_message` → the text is updated in the feed
  (`seq` grows) → the widget updates the bubble;
- **`/del`** as a reply to their own message → the relay marks the row `deleted` → the widget removes the bubble;
- **`/clear`** → the user's entire feed is marked `deleted` + the topic is deleted (`deleteForumTopic`) + the ticket is
  reset → the widget clears, and the next message creates a fresh topic;
- **`/help`** → the bot prints a command hint into the topic (it does not go to the user's feed);
- **`/close`** → close the ticket.

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
