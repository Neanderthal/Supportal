"""Ph1 — ingress via the HTTP route (TestClient) with a mocked TelegramOps.

We check: the HMAC gate on /v1/messages (200/401), find-or-create of the ticket, creating the topic once per ticket,
the message header (name + meta context) and reuse of an open ticket. aiogram/network not needed —
the relay is assembled with a fake TelegramOps (async ensure_topic/send_to_topic).
"""
import os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal.api import build_router
from supportal.models import IngressMessage
from supportal.relay import Relay, sign
from supportal.store import Store

_INGRESS_SECRET = "ing-secret"


class _FakeTG:
    """A TelegramOps double: keeps a list of created topics and sent messages."""

    def __init__(self) -> None:
        self.topics: list[tuple[int, str]] = []
        self.sent: list[tuple[int, str]] = []
        self._next = 100

    async def ensure_topic(self, title: str) -> int:
        self._next += 1
        self.topics.append((self._next, title))
        return self._next

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        self.sent.append((thread_id, text))

    async def close(self) -> None:
        return None


def _client(tg: _FakeTG) -> TestClient:
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_"), "t.db")
    store = Store(f"sqlite:///{path}")
    store.bootstrap()
    relay = Relay(store, tg, "http://product/support/push", "cb-secret")
    app = FastAPI()
    app.include_router(build_router(relay, _INGRESS_SECRET))
    return TestClient(app)


def _post(client: TestClient, msg: IngressMessage, secret: str = _INGRESS_SECRET):
    body = msg.model_dump_json().encode()
    return client.post("/v1/messages", content=body,
                       headers={"Content-Type": "application/json", "X-Supportal-Signature": sign(body, secret)})


def test_ingress_valid_signature_creates_ticket_and_topic():
    tg = _FakeTG()
    client = _client(tg)
    r = _post(client, IngressMessage(user_ref="eng@x", text="the password reset email doesn't arrive",
                                     display_name="Ivanov", meta={"product": "web", "page": "Settings"}))
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["ticket_id"]
    assert data["thread_id"] == tg.topics[0][0]
    # one topic per ticket, one sent message; the header carries name + context, the body — the text
    assert len(tg.topics) == 1 and len(tg.sent) == 1
    sent_text = tg.sent[0][1]
    assert "Ivanov" in sent_text and "product=web" in sent_text and "the password reset email doesn't arrive" in sent_text


def test_ingress_bad_signature_401():
    client = _client(_FakeTG())
    body = IngressMessage(user_ref="eng@x", text="x").model_dump_json().encode()
    r = client.post("/v1/messages", content=body, headers={"X-Supportal-Signature": "deadbeef"})
    assert r.status_code == 401


def test_ingress_missing_signature_401():
    client = _client(_FakeTG())
    body = IngressMessage(user_ref="eng@x", text="x").model_dump_json().encode()
    r = client.post("/v1/messages", content=body)
    assert r.status_code == 401


def test_ingress_reuses_open_ticket_one_topic():
    tg = _FakeTG()
    client = _client(tg)
    r1 = _post(client, IngressMessage(user_ref="eng@x", text="first"))
    r2 = _post(client, IngressMessage(user_ref="eng@x", text="second"))
    assert r1.status_code == 200 and r2.status_code == 200
    # the topic is created once; both messages went into it, the ticket is the same
    assert len(tg.topics) == 1 and len(tg.sent) == 2
    assert r1.json()["ticket_id"] == r2.json()["ticket_id"]
    assert r2.json()["thread_id"] == tg.topics[0][0]


def test_health():
    client = _client(_FakeTG())
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "service": "supportal"}
