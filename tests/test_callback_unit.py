"""Ph1 — reverse path: operator reply/command → callback to the product (HMAC), httpx mocked.

We check: on_operator_message/on_operator_command assemble an OperatorEvent, sign the body with HMAC and
POST it to the callback URL; an unknown topic is ignored; /close closes the ticket and sends ticket_closed;
an unknown command is ignored; a callback failure is fail-soft (not propagated). No network/aiogram.
"""
import asyncio, hmac, json, os, tempfile

import httpx

from supportal.relay import Relay, sign
from supportal.store import Store

_CB_URL = "http://product/support/push"
_CB_SECRET = "cb-secret"


class _FakeTG:
    async def ensure_topic(self, title: str) -> int:
        return 1

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def close(self) -> None:
        return None


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status


class _FakeClient:
    """Records _dispatch POSTs and returns 200. Overridden in the fail-path test."""

    posts: list[tuple] = []

    def __init__(self, *a, **k) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        _FakeClient.posts.append((url, content, headers))
        return _Resp(200)


class _RaisingClient:
    def __init__(self, *a, **k) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise RuntimeError("callback down")


def _store() -> Store:
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_"), "t.db")
    s = Store(f"sqlite:///{path}")
    s.bootstrap()
    return s


def _relay(store: Store) -> Relay:
    return Relay(store, _FakeTG(), _CB_URL, _CB_SECRET)


def test_operator_message_dispatches_signed_callback(monkeypatch):
    _FakeClient.posts.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    store = _store()
    t = store.create("eng@x", "Ivanov")
    store.attach_thread(t.ticket_id, 55)
    asyncio.run(_relay(store).on_operator_message(55, "check the tank breather", "support1"))
    assert len(_FakeClient.posts) == 1
    url, content, headers = _FakeClient.posts[0]
    assert url == _CB_URL
    assert hmac.compare_digest(headers["X-Supportal-Signature"], sign(content, _CB_SECRET))
    ev = json.loads(content)
    assert ev == {"event": "message", "user_ref": "eng@x", "ticket_id": t.ticket_id,
                  "text": "check the tank breather", "operator": "support1"}


def test_operator_message_unknown_thread_no_dispatch(monkeypatch):
    _FakeClient.posts.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    asyncio.run(_relay(_store()).on_operator_message(999, "reply into the void", "support1"))
    assert _FakeClient.posts == []


def test_operator_close_closes_ticket_and_dispatches(monkeypatch):
    _FakeClient.posts.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    store = _store()
    t = store.create("eng@x", "Ivanov")
    store.attach_thread(t.ticket_id, 77)
    asyncio.run(_relay(store).on_operator_command(77, "/close", "support1"))
    assert store.get_open_by_user("eng@x") is None                       # ticket closed
    assert len(_FakeClient.posts) == 1
    ev = json.loads(_FakeClient.posts[0][1])
    assert ev["event"] == "ticket_closed" and ev["text"] == "" and ev["operator"] == "support1"


def test_operator_unknown_command_ignored(monkeypatch):
    _FakeClient.posts.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    store = _store()
    t = store.create("eng@x", "Ivanov")
    store.attach_thread(t.ticket_id, 88)
    asyncio.run(_relay(store).on_operator_command(88, "/ban", "support1"))
    # v1 knows only /close: the ticket stays open, no callback is sent
    assert store.get_open_by_user("eng@x") is not None
    assert _FakeClient.posts == []


def test_dispatch_failsoft_on_callback_error(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)
    store = _store()
    t = store.create("eng@x", "Ivanov")
    store.attach_thread(t.ticket_id, 11)
    # a callback failure must not propagate (the operator reply is lost, but the relay stays alive)
    asyncio.run(_relay(store).on_operator_message(11, "any text", "support1"))


def test_operator_command_unknown_thread_noop(monkeypatch):
    _FakeClient.posts.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    # command in a thread with no ticket → get_by_thread None → no-op: no dispatch, no crash
    asyncio.run(_relay(_store()).on_operator_command(12345, "/close", "support1"))
    assert _FakeClient.posts == []


class _RaisingTG:
    """TelegramOps double whose delete_topic raises (operator bot lacks can_manage_topics)."""
    async def ensure_topic(self, title: str) -> int:
        return 1

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def delete_topic(self, thread_id: int) -> None:
        raise RuntimeError("no can_manage_topics")

    async def close(self) -> None:
        return None


def test_clear_failsoft_when_delete_topic_raises():
    # /clear with a failing delete_topic must stay fail-soft: the feed is still cleared and the ticket reset
    store = _store()
    t = store.create("eng@x", "Ivanov")
    store.attach_thread(t.ticket_id, 55)
    store.add_message(t.ticket_id, "eng@x", "in", "Ivanov", "hi")
    relay = Relay(store, _RaisingTG(), "", "")          # contour A (no callback) + raising delete_topic
    asyncio.run(relay.on_operator_command(55, "/clear", "Operator"))
    rows = store.changes_since("eng@x", 0)
    assert rows and all(r.deleted for r in rows)        # feed cleared despite the delete_topic failure
    assert store.get_open_by_user("eng@x") is None      # ticket reset


def test_relay_satisfies_operator_handler_protocol():
    # arch review #7: TelegramOps now calls ONE handler with structured events; Relay's on_message/on_media/
    # on_command/on_edit/on_delete unpack the event and delegate to the on_operator_* domain methods.
    from supportal.telegram_ops import (
        OperatorCommand,
        OperatorDelete,
        OperatorEdit,
        OperatorMediaReply,
        OperatorReply,
    )
    store = _store()
    t = store.create("eng@x", "Ivanov")
    store.attach_thread(t.ticket_id, 55)
    relay = Relay(store, _FakeTG(), "", "")             # contour A (no product callback)

    asyncio.run(relay.on_message(OperatorReply(55, "a reply", "support1", 777)))
    assert store.changes_since("eng@x", 0)[-1].text == "a reply"       # → on_operator_message
    asyncio.run(relay.on_edit(OperatorEdit(777, "edited reply")))
    assert store.changes_since("eng@x", 0)[-1].text == "edited reply"  # → on_operator_edit
    asyncio.run(relay.on_delete(OperatorDelete(777)))
    assert store.changes_since("eng@x", 0)[-1].deleted is True         # → on_operator_delete
    asyncio.run(relay.on_command(OperatorCommand(55, "/close", "support1")))
    assert store.get_by_thread(55).status == "closed"                 # → on_operator_command
    # on_media adapter delegates to on_operator_media (behavior gated by SUPPORT_ATTACHMENTS, tested elsewhere)
    asyncio.run(relay.on_media(OperatorMediaReply(55, b"x", "f.bin", "application/octet-stream", "", "op", 0)))
