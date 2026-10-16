"""Reference product adapter — contour B ("via the product").

A minimal, dependency-light illustration of how a PRODUCT backend integrates supportal when it wants the relay
to deliver operator replies to it (instead of the browser widget polling directly). Two halves:

  1. Product → relay: forward a user's message to the relay ingress, HMAC-signed with SUPPORT_INGRESS_SECRET.
  2. Relay → product: receive the operator-reply callback, verify the HMAC, push it to the user's live
     session(s), and report how many were reached via `delivered` (drives the relay's outbox retry).

This file is intentionally self-contained (stdlib `hmac` + FastAPI) and framework-agnostic in spirit — port
the signing/verifying to any language. Run it alongside the relay; point SUPPORT_PRODUCT_CALLBACK_URL at
`/support/push` here. See docs/api.md §1–2 for the full contract.

    uv run uvicorn examples.product_adapter:app --port 9000
"""
from __future__ import annotations

import hashlib
import hmac
import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

RELAY_INGRESS_URL = os.environ.get("RELAY_INGRESS_URL", "http://localhost:8500/v1/messages")
INGRESS_SECRET = os.environ.get("SUPPORT_INGRESS_SECRET", "")     # product → relay (we sign)
CALLBACK_SECRET = os.environ.get("SUPPORT_CALLBACK_SECRET", "")   # relay → product (we verify)

app = FastAPI(title="supportal product adapter (reference)")

# A toy "who is online" registry. A real product keeps live sessions (WebSocket/SSE) keyed by user_ref.
_live_sessions: dict[str, list] = {}


def _sign(body: bytes, secret: str) -> str:
    """HMAC-SHA256 hex of the raw body — the exact scheme both sides use (relay.sign)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class UserMessage(BaseModel):
    user_ref: str
    text: str
    display_name: str = ""


@app.post("/product/send")
async def product_send(msg: UserMessage) -> dict:
    """1) Product → relay: a logged-in user wrote to support → forward it to the relay ingress (signed)."""
    body = msg.model_dump_json().encode()
    headers = {"Content-Type": "application/json", "X-Supportal-Signature": _sign(body, INGRESS_SECRET)}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(RELAY_INGRESS_URL, content=body, headers=headers)
    resp.raise_for_status()
    return resp.json()      # {ok, ticket_id, thread_id, message_id}


@app.post("/support/push")
async def support_push(request: Request, x_supportal_signature: str = Header("")) -> dict:
    """2) Relay → product: an operator reply (or ticket_closed). Verify the HMAC, deliver to live sessions,
    and report `delivered` so the relay knows whether to retry (outbox) for an offline user."""
    body = await request.body()
    if not CALLBACK_SECRET or not hmac.compare_digest(x_supportal_signature, _sign(body, CALLBACK_SECRET)):
        raise HTTPException(status_code=401, detail="bad signature")
    event = (await request.json()) if body else {}
    sessions = _live_sessions.get(event.get("user_ref", ""), [])
    # A real product would push `event["text"]` into each session here (WebSocket/SSE).
    delivered = len(sessions)
    return {"ok": True, "delivered": delivered}   # delivered=0 → the relay keeps the reply in its outbox
