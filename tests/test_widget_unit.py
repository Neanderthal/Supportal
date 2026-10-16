"""Relay widget API (token-gated polling by seq) + feed + edit-sync/`/del` + access token.

We check: token round-trip; store.add_message/changes_since (seq cursor); edit/delete by tg_message_id
(text edit + soft delete, seq growth); HTTP gate /v1/widget/{send,messages} (401; shape/order;
deleted in the output); product-callback dormancy without callback_url. aiogram/network not needed (fake TG).
"""
import asyncio
import os
import tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal.relay import Relay
from supportal.store import Store
from supportal.tokens import mint_widget_token, verify_widget_token
from supportal.widget_api import build_widget_router

_SECRET = "widget-secret-xyz"


class _FakeTG:
    def __init__(self) -> None:
        self._n = 100
        self.sent: list[tuple[int, str]] = []
        self.deleted_topics: list[int] = []

    async def ensure_topic(self, title: str) -> int:
        self._n += 1
        return self._n

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        self.sent.append((thread_id, text))

    async def delete_topic(self, thread_id: int) -> None:
        self.deleted_topics.append(thread_id)

    async def close(self) -> None:
        return None


def _build(callback_url: str = ""):
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_widget_"), "t.db")
    store = Store(f"sqlite:///{path}")
    store.bootstrap()
    tg = _FakeTG()
    relay = Relay(store, tg, callback_url, "cb-secret")
    app = FastAPI()
    app.include_router(build_widget_router(relay, _SECRET))
    return relay, store, tg, TestClient(app)


def _bearer(user_ref: str, secret: str = _SECRET, ttl: int = 900) -> dict:
    return {"Authorization": "Bearer " + mint_widget_token(user_ref, secret, ttl)}


# ── token ──────────────────────────────────────────────────────────────────────
def test_token_roundtrip_and_failures():
    t = mint_widget_token("eng@x", _SECRET, 60)
    assert verify_widget_token(t, _SECRET) == "eng@x"
    assert verify_widget_token(t, "other-secret") is None
    assert verify_widget_token(t[:-1] + ("0" if t[-1] != "0" else "1"), _SECRET) is None
    assert verify_widget_token(mint_widget_token("a", _SECRET, -1), _SECRET) is None
    assert verify_widget_token(t, "") is None and verify_widget_token("", _SECRET) is None
    assert mint_widget_token("a", "", 60) == ""
    # malformed structure (not 3 dot-parts) → None
    assert verify_widget_token("only.two", _SECRET) is None
    # valid signature but a ref that decodes to invalid UTF-8 → except-branch returns None (no crash)
    import hashlib, hmac
    _msg = "_w.9999999999"                   # "_w" = b64url(b"\xff"); decoding to str raises UnicodeDecodeError
    _sig = hmac.new(_SECRET.encode(), _msg.encode(), hashlib.sha256).hexdigest()
    assert verify_widget_token(f"{_msg}.{_sig}", _SECRET) is None


# ── store: feed + seq cursor ──────────────────────────────────────────────────────
def test_store_ledger_and_seq_cursor():
    _, store, _, _ = _build()
    m1 = store.add_message("t1", "eng@x", "in", "Ivanov", "first")
    m2 = store.add_message("t1", "eng@x", "out", "Operator", "reply")
    store.add_message("t1", "other@y", "in", "Petrov", "someone else's")   # a different user_ref — won't show up
    rows = store.changes_since("eng@x", 0)
    assert [r.id for r in rows] == [m1, m2]
    assert [r.direction for r in rows] == ["in", "out"]
    assert rows[0].seq < rows[1].seq                              # seq is monotonic
    tail = store.changes_since("eng@x", rows[0].seq)              # cursor after the first
    assert [r.id for r in tail] == [m2]


def test_store_edit_and_delete_by_tg():
    _, store, _, _ = _build()
    store.add_message("t1", "eng@x", "out", "Operator", "check the breather", tg_message_id=555)
    seq0 = store.changes_since("eng@x", 0)[-1].seq
    # edit-sync: a text edit bumps seq → shows up in changes_since after the previous cursor
    store.update_message_by_tg(555, "check the breather and the level")
    ch = store.changes_since("eng@x", seq0)
    assert len(ch) == 1 and ch[0].text == "check the breather and the level" and ch[0].deleted is False
    seq1 = ch[0].seq
    # /del: soft delete — deleted=1, seq higher still
    store.delete_message_by_tg(555)
    ch2 = store.changes_since("eng@x", seq1)
    assert len(ch2) == 1 and ch2[0].deleted is True
    # a non-existent tg → no-op
    assert store.update_message_by_tg(999, "x") is None
    assert store.delete_message_by_tg(999) is None


# ── HTTP gate ─────────────────────────────────────────────────────────────────────
def test_widget_send_requires_token():
    _, _, _, client = _build()
    assert client.post("/v1/widget/send", json={"text": "hi"}).status_code == 401
    assert client.post("/v1/widget/send", json={"text": "hi"},
                       headers={"Authorization": "Bearer deadbeef"}).status_code == 401


def test_widget_messages_requires_token():
    _, _, _, client = _build()
    assert client.get("/v1/widget/messages?since=0").status_code == 401


def test_widget_send_creates_ticket_and_message():
    _, _, tg, client = _build()
    r = client.post("/v1/widget/send", json={"text": "no pressure"}, headers=_bearer("eng@x"))
    assert r.status_code == 200
    mid = r.json()["message_id"]
    assert mid > 0 and len(tg.sent) == 1 and "no pressure" in tg.sent[0][1]
    poll = client.get("/v1/widget/messages?since=0", headers=_bearer("eng@x")).json()
    assert poll["cursor"] > 0
    assert [m["direction"] for m in poll["messages"]] == ["in"]
    assert poll["messages"][0]["text"] == "no pressure" and poll["messages"][0]["deleted"] is False


def test_widget_empty_text_400():
    _, _, _, client = _build()
    assert client.post("/v1/widget/send", json={"text": "   "}, headers=_bearer("eng@x")).status_code == 400


def test_operator_reply_poll_and_callback_dormant():
    relay, store, tg, client = _build(callback_url="")   # callback empty → _dispatch dormant
    client.post("/v1/widget/send", json={"text": "first"}, headers=_bearer("eng@x"))
    thread_id = store.get_open_by_user("eng@x").thread_id
    asyncio.run(relay.on_operator_message(thread_id, "looking into it, one minute", "support1", 777))
    poll = client.get("/v1/widget/messages?since=0", headers=_bearer("eng@x")).json()
    assert [m["direction"] for m in poll["messages"]] == ["in", "out"]
    # the operator's real name ("support1") does NOT reach the user — always the anonymized "Operator"
    assert poll["messages"][1]["author"] == "Operator" and poll["messages"][1]["text"] == "looking into it, one minute"
    after = poll["cursor"]
    assert client.get(f"/v1/widget/messages?since={after}", headers=_bearer("eng@x")).json()["messages"] == []


def test_edit_and_delete_surface_in_poll():
    relay, store, _, client = _build()
    client.post("/v1/widget/send", json={"text": "question"}, headers=_bearer("eng@x"))
    thread_id = store.get_open_by_user("eng@x").thread_id
    asyncio.run(relay.on_operator_message(thread_id, "breather", "Operator", 555))
    cur = client.get("/v1/widget/messages?since=0", headers=_bearer("eng@x")).json()["cursor"]
    # the operator edited their message in Telegram → edit-sync
    asyncio.run(relay.on_operator_edit(555, "check the breather and the oil level"))
    ch = client.get(f"/v1/widget/messages?since={cur}", headers=_bearer("eng@x")).json()
    assert len(ch["messages"]) == 1 and ch["messages"][0]["text"] == "check the breather and the oil level"
    assert ch["messages"][0]["deleted"] is False
    cur2 = ch["cursor"]
    # the operator recalled the message (/del) → deleted=1
    asyncio.run(relay.on_operator_delete(555))
    ch2 = client.get(f"/v1/widget/messages?since={cur2}", headers=_bearer("eng@x")).json()
    assert len(ch2["messages"]) == 1 and ch2["messages"][0]["deleted"] is True


def test_wrong_user_token_sees_only_own_messages():
    _, _, _, client = _build()
    client.post("/v1/widget/send", json={"text": "eng secret"}, headers=_bearer("eng@x"))
    other = client.get("/v1/widget/messages?since=0", headers=_bearer("intruder@z")).json()
    assert other["messages"] == []


def test_store_clear_marks_all_deleted():
    _, store, _, _ = _build()
    store.add_message("t1", "eng@x", "in", "Ivanov", "a")
    store.add_message("t1", "eng@x", "out", "Operator", "b")
    n = store.clear_messages("eng@x")
    assert n == 2
    ch = store.changes_since("eng@x", 0)
    assert len(ch) == 2 and all(m.deleted for m in ch)   # the whole feed marked deleted


def test_operator_clear_wipes_ledger_and_deletes_topic():
    relay, store, tg, client = _build()
    client.post("/v1/widget/send", json={"text": "question"}, headers=_bearer("eng@x"))
    thread_id = store.get_open_by_user("eng@x").thread_id
    asyncio.run(relay.on_operator_message(thread_id, "reply", "Operator", 42))
    # /clear → clear the feed + delete the topic + reset the ticket
    asyncio.run(relay.on_operator_command(thread_id, "/clear", "Operator"))
    poll = client.get("/v1/widget/messages?since=0", headers=_bearer("eng@x")).json()
    assert poll["messages"] and all(m["deleted"] for m in poll["messages"])   # all deleted → the widget clears
    assert tg.deleted_topics == [thread_id]                                    # Telegram topic deleted
    assert store.get_open_by_user("eng@x") is None                            # ticket reset (closed)
