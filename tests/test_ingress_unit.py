"""Ф1 — ingress через HTTP-роут (TestClient) с замоканным TelegramOps.

Проверяем: HMAC-гейт /v1/messages (200/401), find-or-create тикета, создание топика один раз на тикет,
шапку сообщения (имя + контекст meta) и переиспользование открытого тикета. aiogram/сеть не нужны —
relay собирается с фейковым TelegramOps (async ensure_topic/send_to_topic).
"""
import os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dali_support.api import build_router
from dali_support.models import IngressMessage
from dali_support.relay import Relay, sign
from dali_support.store import Store

_INGRESS_SECRET = "ing-secret"


class _FakeTG:
    """Двойник TelegramOps: ведёт список созданных топиков и отправленных сообщений."""

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
    path = os.path.join(tempfile.mkdtemp(prefix="dali_support_"), "t.db")
    store = Store(f"sqlite:///{path}")
    store.bootstrap()
    relay = Relay(store, tg, "http://product/support/push", "cb-secret")
    app = FastAPI()
    app.include_router(build_router(relay, _INGRESS_SECRET))
    return TestClient(app)


def _post(client: TestClient, msg: IngressMessage, secret: str = _INGRESS_SECRET):
    body = msg.model_dump_json().encode()
    return client.post("/v1/messages", content=body,
                       headers={"Content-Type": "application/json", "X-Dali-Signature": sign(body, secret)})


def test_ingress_valid_signature_creates_ticket_and_topic():
    tg = _FakeTG()
    client = _client(tg)
    r = _post(client, IngressMessage(user_ref="eng@x", text="не приходит письмо для сброса пароля",
                                     display_name="Иванов", meta={"product": "web", "page": "Настройки"}))
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["ticket_id"]
    assert data["thread_id"] == tg.topics[0][0]
    # один топик на тикет, одно отправленное сообщение; шапка несёт имя + контекст, тело — текст
    assert len(tg.topics) == 1 and len(tg.sent) == 1
    sent_text = tg.sent[0][1]
    assert "Иванов" in sent_text and "product=web" in sent_text and "не приходит письмо для сброса пароля" in sent_text


def test_ingress_bad_signature_401():
    client = _client(_FakeTG())
    body = IngressMessage(user_ref="eng@x", text="x").model_dump_json().encode()
    r = client.post("/v1/messages", content=body, headers={"X-Dali-Signature": "deadbeef"})
    assert r.status_code == 401


def test_ingress_missing_signature_401():
    client = _client(_FakeTG())
    body = IngressMessage(user_ref="eng@x", text="x").model_dump_json().encode()
    r = client.post("/v1/messages", content=body)
    assert r.status_code == 401


def test_ingress_reuses_open_ticket_one_topic():
    tg = _FakeTG()
    client = _client(tg)
    r1 = _post(client, IngressMessage(user_ref="eng@x", text="первое"))
    r2 = _post(client, IngressMessage(user_ref="eng@x", text="второе"))
    assert r1.status_code == 200 and r2.status_code == 200
    # топик создаётся один раз; оба сообщения ушли в него, тикет тот же
    assert len(tg.topics) == 1 and len(tg.sent) == 2
    assert r1.json()["ticket_id"] == r2.json()["ticket_id"]
    assert r2.json()["thread_id"] == tg.topics[0][0]


def test_health():
    client = _client(_FakeTG())
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "service": "dali-support"}
