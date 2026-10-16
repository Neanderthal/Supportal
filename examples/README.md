# Examples

## `demo/` — local demo with a mocked Telegram side

A self-contained web page with the embedded Supportal widget and a **mock operator
console**, so you can see the full round-trip without a real Telegram bot or supergroup.
`TelegramOps` is replaced by an in-memory fake; the conversation lives in an in-memory
SQLite store.

### Run

```bash
uv run python examples/demo/demo_server.py
```

Then open:

- <http://localhost:8600/> — the user-facing page; the widget is the **💬** button
  (bottom-left). Open it and send a message.
- <http://localhost:8600/operator> — the mock "Telegram topic" console. Reply as the
  operator; the answer reaches the widget within ~4 s (polling). You can also **edit** or
  **delete** an operator message and watch it sync in the widget (this is what
  `edited_message` / `/del` do with real Telegram).

### What it wires up

- `POST /support/config` — mints a short-lived widget token (the product's job).
- `/support-api/v1/widget/{send,messages}` — the real relay widget API (in production a
  reverse proxy strips `/support-api`).
- `/op/api/*` — demo-only endpoints that stand in for the Telegram operator side; they
  call `relay.on_operator_message` / `on_operator_edit` / `on_operator_delete` /
  `on_operator_command`.

This is a demo, not production: fixed `user_ref`, in-memory DB (resets on restart), a
hard-coded widget secret, and no authentication on the operator console.
