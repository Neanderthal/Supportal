"""Виджет-API relay (token-gated polling по seq) + лента + edit-sync/`/del` + токен доступа.

Проверяем: round-trip токена; store.add_message/changes_since (курсор seq); edit/delete по tg_message_id
(правка текста + мягкое удаление, рост seq); HTTP-гейт /v1/widget/{send,messages} (401; форма/порядок;
deleted в выдаче); дормантность product-callback без callback_url. aiogram/сеть не нужны (фейковый TG).
"""
import asyncio
import os
import tempfile

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
    assert verify_widget_token(t, "other-secret") is None
    assert verify_widget_token(t[:-1] + ("0" if t[-1] != "0" else "1"), _SECRET) is None
    assert verify_widget_token(mint_widget_token("a", _SECRET, -1), _SECRET) is None
    assert verify_widget_token(t, "") is None and verify_widget_token("", _SECRET) is None
    assert mint_widget_token("a", "", 60) == ""


# ── store: лента + курсор seq ───────────────────────────────────────────────────
def test_store_ledger_and_seq_cursor():
    _, store, _, _ = _build()
    m1 = store.add_message("t1", "eng@x", "in", "Иванов", "первое")
    m2 = store.add_message("t1", "eng@x", "out", "Оператор", "ответ")
    store.add_message("t1", "other@y", "in", "Петров", "чужое")   # другой user_ref — не попадёт
    rows = store.changes_since("eng@x", 0)
    assert [r.id for r in rows] == [m1, m2]
    assert [r.direction for r in rows] == ["in", "out"]
    assert rows[0].seq < rows[1].seq                              # seq монотонен
    tail = store.changes_since("eng@x", rows[0].seq)              # курсор после первого
    assert [r.id for r in tail] == [m2]


def test_store_edit_and_delete_by_tg():
    _, store, _, _ = _build()
    store.add_message("t1", "eng@x", "out", "Оператор", "сапун проверьте", tg_message_id=555)
    seq0 = store.changes_since("eng@x", 0)[-1].seq
    # edit-sync: правка текста поднимает seq → попадает в changes_since после прежнего курсора
    store.update_message_by_tg(555, "проверьте сапун и уровень")
    ch = store.changes_since("eng@x", seq0)
    assert len(ch) == 1 and ch[0].text == "проверьте сапун и уровень" and ch[0].deleted is False
    seq1 = ch[0].seq
    # /del: мягкое удаление — deleted=1, seq ещё выше
    store.delete_message_by_tg(555)
    ch2 = store.changes_since("eng@x", seq1)
    assert len(ch2) == 1 and ch2[0].deleted is True
    # несуществующий tg → no-op
    assert store.update_message_by_tg(999, "x") is None
    assert store.delete_message_by_tg(999) is None


# ── HTTP-гейт ───────────────────────────────────────────────────────────────────
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
    r = client.post("/v1/widget/send", json={"text": "нет давления"}, headers=_bearer("eng@x"))
    assert r.status_code == 200
    mid = r.json()["message_id"]
    assert mid > 0 and len(tg.sent) == 1 and "нет давления" in tg.sent[0][1]
    poll = client.get("/v1/widget/messages?since=0", headers=_bearer("eng@x")).json()
    assert poll["cursor"] > 0
    assert [m["direction"] for m in poll["messages"]] == ["in"]
    assert poll["messages"][0]["text"] == "нет давления" and poll["messages"][0]["deleted"] is False


def test_widget_empty_text_400():
    _, _, _, client = _build()
    assert client.post("/v1/widget/send", json={"text": "   "}, headers=_bearer("eng@x")).status_code == 400


def test_operator_reply_poll_and_callback_dormant():
    relay, store, tg, client = _build(callback_url="")   # callback пуст → _dispatch дормант
    client.post("/v1/widget/send", json={"text": "первое"}, headers=_bearer("eng@x"))
    thread_id = store.get_open_by_user("eng@x").thread_id
    asyncio.run(relay.on_operator_message(thread_id, "проверяю, минуту", "Оператор", 777))
    poll = client.get("/v1/widget/messages?since=0", headers=_bearer("eng@x")).json()
    assert [m["direction"] for m in poll["messages"]] == ["in", "out"]
    assert poll["messages"][1]["author"] == "Оператор" and poll["messages"][1]["text"] == "проверяю, минуту"
    after = poll["cursor"]
    assert client.get(f"/v1/widget/messages?since={after}", headers=_bearer("eng@x")).json()["messages"] == []


def test_edit_and_delete_surface_in_poll():
    relay, store, _, client = _build()
    client.post("/v1/widget/send", json={"text": "вопрос"}, headers=_bearer("eng@x"))
    thread_id = store.get_open_by_user("eng@x").thread_id
    asyncio.run(relay.on_operator_message(thread_id, "сапун", "Оператор", 555))
    cur = client.get("/v1/widget/messages?since=0", headers=_bearer("eng@x")).json()["cursor"]
    # оператор отредактировал своё сообщение в Telegram → edit-sync
    asyncio.run(relay.on_operator_edit(555, "проверьте сапун и уровень масла"))
    ch = client.get(f"/v1/widget/messages?since={cur}", headers=_bearer("eng@x")).json()
    assert len(ch["messages"]) == 1 and ch["messages"][0]["text"] == "проверьте сапун и уровень масла"
    assert ch["messages"][0]["deleted"] is False
    cur2 = ch["cursor"]
    # оператор отозвал сообщение (/del) → deleted=1
    asyncio.run(relay.on_operator_delete(555))
    ch2 = client.get(f"/v1/widget/messages?since={cur2}", headers=_bearer("eng@x")).json()
    assert len(ch2["messages"]) == 1 and ch2["messages"][0]["deleted"] is True


def test_wrong_user_token_sees_only_own_messages():
    _, _, _, client = _build()
    client.post("/v1/widget/send", json={"text": "секрет eng"}, headers=_bearer("eng@x"))
    other = client.get("/v1/widget/messages?since=0", headers=_bearer("intruder@z")).json()
    assert other["messages"] == []


def test_store_clear_marks_all_deleted():
    _, store, _, _ = _build()
    store.add_message("t1", "eng@x", "in", "Иванов", "a")
    store.add_message("t1", "eng@x", "out", "Оператор", "b")
    n = store.clear_messages("eng@x")
    assert n == 2
    ch = store.changes_since("eng@x", 0)
    assert len(ch) == 2 and all(m.deleted for m in ch)   # вся лента помечена удалённой


def test_operator_clear_wipes_ledger_and_deletes_topic():
    relay, store, tg, client = _build()
    client.post("/v1/widget/send", json={"text": "вопрос"}, headers=_bearer("eng@x"))
    thread_id = store.get_open_by_user("eng@x").thread_id
    asyncio.run(relay.on_operator_message(thread_id, "ответ", "Оператор", 42))
    # /clear → очистка ленты + удаление топика + сброс тикета
    asyncio.run(relay.on_operator_command(thread_id, "/clear", "Оператор"))
    poll = client.get("/v1/widget/messages?since=0", headers=_bearer("eng@x")).json()
    assert poll["messages"] and all(m["deleted"] for m in poll["messages"])   # всё удалено → виджет очистит
    assert tg.deleted_topics == [thread_id]                                    # топик Telegram удалён
    assert store.get_open_by_user("eng@x") is None                            # тикет сброшен (закрыт)
