"""Виджет-API relay (token-gated polling) + лента сообщений + токен доступа.

Проверяем: round-trip токена (mint↔verify, подделка/просрочка/пустой секрет), store.add_message/
messages_since (курсор), HTTP-гейт /v1/widget/{send,messages} (401 без/с плохим токеном, 200 с валидным),
запись in/out в ленту и дормантность product-callback без callback_url. aiogram/сеть не нужны (фейковый TG).
"""
import asyncio
import os
import tempfile
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dali_support.relay import Relay
from dali_support.store import Store
from dali_support.tokens import mint_widget_token, verify_widget_token
from dali_support.widget_api import build_widget_router

_SECRET = "widget-secret-xyz"


class _FakeTG:
    def __init__(self) -> None:
        self._n = 100
        self.sent: list[tuple[int, str]] = []

    async def ensure_topic(self, title: str) -> int:
        self._n += 1
        return self._n

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        self.sent.append((thread_id, text))

    async def close(self) -> None:
        return None


def _build(callback_url: str = ""):
    path = os.path.join(tempfile.mkdtemp(prefix="dali_widget_"), "t.db")
    store = Store(f"sqlite:///{path}")
    store.bootstrap()
    tg = _FakeTG()
    relay = Relay(store, tg, callback_url, "cb-secret")
    app = FastAPI()
    app.include_router(build_widget_router(relay, _SECRET))
    return relay, store, tg, TestClient(app)


def _bearer(user_ref: str, secret: str = _SECRET, ttl: int = 900) -> dict:
    return {"Authorization": "Bearer " + mint_widget_token(user_ref, secret, ttl)}


# ── токен ────────────────────────────────────────────────────────────────────
def test_token_roundtrip_and_failures():
    t = mint_widget_token("eng@x", _SECRET, 60)
    assert verify_widget_token(t, _SECRET) == "eng@x"
    assert verify_widget_token(t, "other-secret") is None          # чужой секрет
    assert verify_widget_token(t[:-1] + ("0" if t[-1] != "0" else "1"), _SECRET) is None  # подделка подписи
    assert verify_widget_token(mint_widget_token("a", _SECRET, -1), _SECRET) is None       # просрочен
    assert verify_widget_token(t, "") is None and verify_widget_token("", _SECRET) is None  # пустой секрет/токен
    assert mint_widget_token("a", "", 60) == ""                     # нет секрета → пустой токен


# ── store: лента ───────────────────────────────────────────────────────────────
def test_store_messages_since_cursor():
    _, store, _, _ = _build()
    m1 = store.add_message("t1", "eng@x", "in", "Иванов", "первое")
    m2 = store.add_message("t1", "eng@x", "out", "Оператор", "ответ")
    store.add_message("t1", "other@y", "in", "Петров", "чужое")   # другой user_ref — не попадёт
    allm = store.messages_since("eng@x", 0)
    assert [m.id for m in allm] == [m1, m2]
    assert [m.direction for m in allm] == ["in", "out"]
    tail = store.messages_since("eng@x", m1)                      # курсор после первого
    assert [m.id for m in tail] == [m2]


# ── HTTP-гейт ───────────────────────────────────────────────────────────────────
def test_widget_send_requires_token():
    _, _, _, client = _build()
    assert client.post("/v1/widget/send", json={"text": "hi"}).status_code == 401
    bad = {"Authorization": "Bearer deadbeef"}
    assert client.post("/v1/widget/send", json={"text": "hi"}, headers=bad).status_code == 401


def test_widget_messages_requires_token():
    _, _, _, client = _build()
    assert client.get("/v1/widget/messages?after=0").status_code == 401


def test_widget_send_creates_ticket_and_message():
    _, store, tg, client = _build()
    r = client.post("/v1/widget/send", json={"text": "нет давления"}, headers=_bearer("eng@x"))
    assert r.status_code == 200
    mid = r.json()["message_id"]
    assert mid > 0
    assert len(tg.sent) == 1 and "нет давления" in tg.sent[0][1]   # ушло в топик
    poll = client.get("/v1/widget/messages?after=0", headers=_bearer("eng@x")).json()
    assert poll["last_id"] == mid
    assert [m["direction"] for m in poll["messages"]] == ["in"]
    assert poll["messages"][0]["text"] == "нет давления"


def test_widget_empty_text_400():
    _, _, _, client = _build()
    assert client.post("/v1/widget/send", json={"text": "   "}, headers=_bearer("eng@x")).status_code == 400


def test_operator_reply_appears_in_poll_and_callback_dormant():
    relay, store, tg, client = _build(callback_url="")   # callback пуст → _dispatch дормант
    client.post("/v1/widget/send", json={"text": "первое"}, headers=_bearer("eng@x"))
    thread_id = store.get_open_by_user("eng@x").thread_id
    # ответ оператора (в проде — из long-polling топика); callback_url пуст → только запись в ленту, без HTTP
    asyncio.run(relay.on_operator_message(thread_id, "проверяю, минуту", "Оператор"))
    poll = client.get("/v1/widget/messages?after=0", headers=_bearer("eng@x")).json()
    assert [m["direction"] for m in poll["messages"]] == ["in", "out"]
    assert poll["messages"][1]["author"] == "Оператор" and poll["messages"][1]["text"] == "проверяю, минуту"
    # polling с курсором после последнего → пусто, last_id не откатывается
    after = poll["last_id"]
    assert client.get(f"/v1/widget/messages?after={after}", headers=_bearer("eng@x")).json()["messages"] == []


def test_wrong_user_token_sees_only_own_messages():
    _, _, _, client = _build()
    client.post("/v1/widget/send", json={"text": "секрет eng"}, headers=_bearer("eng@x"))
    other = client.get("/v1/widget/messages?after=0", headers=_bearer("intruder@z")).json()
    assert other["messages"] == []   # токен привязан к user_ref → чужую ленту не видно


def test_token_ttl_future_only():
    # свежий токен валиден, токен с ttl<=0 — нет (граница exp)
    assert verify_widget_token(mint_widget_token("a", _SECRET, 1), _SECRET) == "a"
    time.sleep(0)  # без ожидания: ttl=-1 заведомо в прошлом
    assert verify_widget_token(mint_widget_token("a", _SECRET, -1), _SECRET) is None
