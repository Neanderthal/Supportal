"""Local demo: a web page with the embedded Supportal widget and a MOCKED Telegram side.

Run from the repo root:
    uv run python examples/demo/demo_server.py
then open:
    http://localhost:8600/          — user-facing page with the chat widget (💬, bottom-left)
    http://localhost:8600/operator  — mock "Telegram topic" console to reply as the operator

No real Telegram bot/supergroup is used: `TelegramOps` is replaced by an in-memory fake,
so the full round-trip works offline:

    widget  --POST /support-api/v1/widget/send-->  relay  (writes `in` to the feed)
    operator console  --POST /op/api/reply-->  relay.on_operator_message (writes `out`)
    widget  --GET /support-api/v1/widget/messages (poll ~4s)-->  shows the reply

It also demonstrates edit-sync and recall: editing/deleting an operator message in the
console propagates to the widget (seq cursor), exactly as `edited_message` / `/del` would.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the repo's `supportal` package importable when this file is run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# The demo turns the attachments feature on (read live by the widget API via settings).
os.environ.setdefault("SUPPORT_ATTACHMENTS", "on")

from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from supportal.relay import Relay  # noqa: E402
from supportal.store import Store  # noqa: E402
from supportal.tokens import mint_widget_token  # noqa: E402
from supportal.widget_api import build_widget_router  # noqa: E402

DEMO_SECRET = "demo-widget-secret"      # shared by /support/config (mint) and the widget API (verify)
DEMO_USER = "demo@local"                # in production this is the logged-in user's normalized id
HERE = Path(__file__).resolve().parent
WIDGET_DIR = _REPO_ROOT / "supportal" / "widget"


class FakeTelegramOps:
    """In-memory stand-in for `TelegramOps` — no network, no bot token.

    The relay calls `ensure_topic`/`send_to_topic`/`delete_topic`; we only track topic ids.
    The conversation itself lives in the relay's store (`messages` feed), which both the
    widget (via polling) and the mock operator console read.
    """

    def __init__(self) -> None:
        self._next_thread = 1000
        self._next_msg = 5000
        self.threads: dict[int, str] = {}   # thread_id -> title

    async def ensure_topic(self, title: str) -> int:
        self._next_thread += 1
        self.threads[self._next_thread] = title
        return self._next_thread

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None   # the user message is already in the feed; nothing to deliver to real Telegram

    async def send_media(self, thread_id: int, data: bytes, filename: str, mime: str, caption: str = "") -> None:
        return None   # the user's file is already stored by the relay; nothing to deliver to real Telegram

    async def delete_topic(self, thread_id: int) -> None:
        self.threads.pop(thread_id, None)

    async def close(self) -> None:
        return None

    def next_message_id(self) -> int:
        self._next_msg += 1
        return self._next_msg


store = Store("sqlite:///:memory:")
store.bootstrap()
tg = FakeTelegramOps()
relay = Relay(store, tg, callback_url="", callback_secret="")   # contour A only (no product callback)

app = FastAPI(title="supportal-demo")

# The browser talks to the widget API under /support-api (in production a reverse proxy
# strips that prefix to the relay's /v1/widget/*; here we mount it at that path directly).
app.include_router(build_widget_router(relay, DEMO_SECRET), prefix="/support-api")


@app.post("/support/config")
async def support_config() -> dict:
    """Product side: mint a short-lived widget token for the logged-in user (here: DEMO_USER)."""
    return {"api_base": "/support-api", "token": mint_widget_token(DEMO_USER, DEMO_SECRET, ttl=900),
            "ttl": 900, "attachments": True}


# ── mock operator console (stands in for the Telegram forum topic) ──────────────────────
class ReplyIn(BaseModel):
    thread_id: int
    text: str


class EditIn(BaseModel):
    tg_message_id: int
    text: str


class DeleteIn(BaseModel):
    tg_message_id: int


class CloseIn(BaseModel):
    thread_id: int


def _conversation(user_ref: str) -> list[dict]:
    return [{"id": m.id, "direction": m.direction, "author": m.author, "text": m.text,
             "deleted": m.deleted, "tg_message_id": m.tg_message_id,
             "attachment_id": m.attachment_id, "attachment_name": m.attachment_name,
             "attachment_mime": m.attachment_mime}
            for m in store.changes_since(user_ref, 0)]


@app.get("/op/api/state")
async def op_state() -> dict:
    tickets = []
    for thread_id, title in sorted(tg.threads.items()):
        ticket = store.get_by_thread(thread_id)
        if not ticket:
            continue
        tickets.append({"thread_id": thread_id, "title": title, "user_ref": ticket.user_ref,
                        "status": ticket.status, "messages": _conversation(ticket.user_ref)})
    return {"tickets": tickets}


@app.get("/op/api/attachment/{attachment_id}")
async def op_attachment(attachment_id: int) -> Response:
    """Serve attachment bytes to the operator console (demo-only, no auth — unlike the token-gated widget route)."""
    att = store.get_attachment(attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="not found")
    headers = {}
    if not (att.mime or "").startswith("image/"):
        headers["Content-Disposition"] = f'attachment; filename="{att.filename}"'
    return Response(content=att.data, media_type=att.mime or "application/octet-stream", headers=headers)


@app.post("/op/api/reply")
async def op_reply(body: ReplyIn) -> dict:
    if not store.get_by_thread(body.thread_id):
        raise HTTPException(status_code=404, detail="unknown thread")
    mid = tg.next_message_id()
    await relay.on_operator_message(body.thread_id, body.text, "Operator (demo)", tg_message_id=mid)
    return {"ok": True, "tg_message_id": mid}


@app.post("/op/api/reply_media")
async def op_reply_media(request: Request, thread_id: int, name: str = "") -> dict:
    """Operator sends a file (raw body; Content-Type → mime, ?name= → filename). Stands in for a Telegram photo/document."""
    if not store.get_by_thread(thread_id):
        raise HTTPException(status_code=404, detail="unknown thread")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    mime = (request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
    await relay.on_operator_media(thread_id, data, name or "file", mime, "", "Operator (demo)",
                                  tg_message_id=tg.next_message_id())
    return {"ok": True}


@app.post("/op/api/edit")
async def op_edit(body: EditIn) -> dict:
    await relay.on_operator_edit(body.tg_message_id, body.text)
    return {"ok": True}


@app.post("/op/api/delete")
async def op_delete(body: DeleteIn) -> dict:
    await relay.on_operator_delete(body.tg_message_id)
    return {"ok": True}


@app.post("/op/api/close")
async def op_close(body: CloseIn) -> dict:
    await relay.on_operator_command(body.thread_id, "/close", "Operator (demo)")
    return {"ok": True}


# ── pages + widget static assets ────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (HERE / "index.html").read_text(encoding="utf-8")


@app.get("/operator", response_class=HTMLResponse)
async def operator() -> str:
    return (HERE / "operator.html").read_text(encoding="utf-8")


app.mount("/static", StaticFiles(directory=str(WIDGET_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8600)
