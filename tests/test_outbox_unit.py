"""Ф3b — outbox повторной доставки ответа оператора. Граница A: relay повторяет HTTP-callback, НЕ зная
про сессии продукта — решение «доставлено?» целиком по числу delivered из ответа.

Оффлайн: стор outbox (enqueue/due/mark_*), Relay._post_callback (семантика delivered), _dispatch
(enqueue на промахе, не на успехе), retry_outbox_once (delivered→done, промах→backoff, TTL/max→dead).
Сеть замокана (httpx), aiogram не нужен (TelegramOps — фейк).
"""
import asyncio, os, tempfile

import httpx

from supportal.models import OperatorEvent
from supportal.relay import Relay
from supportal.store import Store

_EV = OperatorEvent(event="message", user_ref="eng@x", ticket_id="t1", text="проверьте сапун", operator="op1")


class _FakeTG:
    async def ensure_topic(self, title):
        return 1

    async def send_to_topic(self, thread_id, text):
        return None

    async def close(self):
        return None


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("нет тела")
        return self._payload


class _Client:
    status = 200
    payload = {"ok": True, "delivered": 1}
    raise_exc = False
    calls = 0

    def __init__(self, *a, **k):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        _Client.calls += 1
        if _Client.raise_exc:
            raise RuntimeError("relay/продукт недоступен")
        return _Resp(_Client.status, _Client.payload)


def _store() -> Store:
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_"), "t.db")
    s = Store(f"sqlite:///{path}")
    s.bootstrap()
    return s


def _relay(store: Store, **kw) -> Relay:
    return Relay(store, _FakeTG(), "http://product/support/push", "cb-secret", **kw)


def _patch(monkeypatch, status=200, payload=None, raise_exc=False):
    _Client.calls = 0
    _Client.status = status
    _Client.payload = payload
    _Client.raise_exc = raise_exc
    monkeypatch.setattr(httpx, "AsyncClient", _Client)


# ── стор ──────────────────────────────────────────────────────────────────────
def test_outbox_enqueue_due_and_delivered():
    s = _store()
    assert s.outbox_pending_count() == 0
    rid = s.enqueue_outbox("t1", "eng@x", "message", "привет", "op1", attempts=1, next_attempt_at=100.0)
    assert rid > 0 and s.outbox_pending_count() == 1
    assert s.outbox_due(now=50.0) == []                 # ещё не дозрело
    due = s.outbox_due(now=150.0)
    assert len(due) == 1 and due[0].ticket_id == "t1" and due[0].attempts == 1
    s.outbox_mark_delivered(due[0].id)
    assert s.outbox_pending_count() == 0 and s.outbox_due(now=150.0) == []


def test_outbox_mark_retry_and_dead():
    s = _store()
    rid = s.enqueue_outbox("t2", "eng@x", "message", "x", "op", attempts=1, next_attempt_at=10.0)
    s.outbox_mark_retry(rid, attempts=2, next_attempt_at=999.0)
    assert s.outbox_due(now=100.0) == []                # перенесено в будущее
    assert s.outbox_due(now=1000.0)[0].attempts == 2
    s.outbox_mark_dead(rid)
    assert s.outbox_pending_count() == 0


# ── _post_callback: семантика delivered ────────────────────────────────────────
def test_post_callback_delivered_semantics(monkeypatch):
    r = _relay(_store())
    _patch(monkeypatch, payload={"ok": True, "delivered": 2})
    assert asyncio.run(r._post_callback(_EV)) is True
    _patch(monkeypatch, payload={"ok": True, "delivered": 0})     # адресат оффлайн
    assert asyncio.run(r._post_callback(_EV)) is False
    _patch(monkeypatch, payload={"ok": True})                     # нет delivered → совместимость → доставлено
    assert asyncio.run(r._post_callback(_EV)) is True
    _patch(monkeypatch, status=500, payload={"delivered": 1})     # 5xx → повторим
    assert asyncio.run(r._post_callback(_EV)) is False
    _patch(monkeypatch, raise_exc=True)                           # сеть упала → повторим
    assert asyncio.run(r._post_callback(_EV)) is False


# ── _dispatch: enqueue только на промахе ────────────────────────────────────────
def test_dispatch_enqueues_on_miss(monkeypatch):
    s = _store()
    _patch(monkeypatch, payload={"delivered": 0})
    asyncio.run(_relay(s)._dispatch(_EV))
    assert s.outbox_pending_count() == 1
    due = s.outbox_due(now=10 ** 12)
    assert due[0].text == "проверьте сапун" and due[0].attempts == 1


def test_dispatch_no_enqueue_on_hit(monkeypatch):
    s = _store()
    _patch(monkeypatch, payload={"delivered": 1})
    asyncio.run(_relay(s)._dispatch(_EV))
    assert s.outbox_pending_count() == 0


# ── retry_outbox_once ───────────────────────────────────────────────────────────
def test_retry_delivers_when_back_online(monkeypatch):
    s = _store()
    s.enqueue_outbox("t", "eng@x", "message", "ответ", "op", attempts=1, next_attempt_at=0.0)
    _patch(monkeypatch, payload={"delivered": 1})       # пользователь вернулся → адаптер запушил
    n = asyncio.run(_relay(s).retry_outbox_once())
    assert n == 1 and s.outbox_pending_count() == 0


def test_retry_reschedules_on_miss(monkeypatch):
    s = _store()
    s.enqueue_outbox("t", "eng@x", "message", "ответ", "op", attempts=1, next_attempt_at=0.0)
    _patch(monkeypatch, payload={"delivered": 0})       # всё ещё оффлайн
    asyncio.run(_relay(s).retry_outbox_once())
    assert s.outbox_pending_count() == 1
    assert s.outbox_due(now=0.0) == []                  # отложено по backoff в будущее
    assert s.outbox_due(now=10 ** 12)[0].attempts == 2  # попытка учтена


def test_retry_gives_up_after_max_attempts(monkeypatch):
    s = _store()
    r = _relay(s, outbox_max_attempts=3)
    s.enqueue_outbox("t", "eng@x", "message", "ответ", "op", attempts=3, next_attempt_at=0.0)
    _patch(monkeypatch, payload={"delivered": 0})
    asyncio.run(r.retry_outbox_once())
    assert s.outbox_pending_count() == 0                # помечено dead
    assert _Client.calls == 0                           # исчерпали попытки → даже не стучались


def test_retry_gives_up_after_ttl(monkeypatch):
    s = _store()
    r = _relay(s, outbox_ttl=0.0)                       # мгновенно просрочено
    s.enqueue_outbox("t", "eng@x", "message", "ответ", "op", attempts=1, next_attempt_at=0.0)
    _patch(monkeypatch, payload={"delivered": 0})
    asyncio.run(r.retry_outbox_once())
    assert s.outbox_pending_count() == 0                # dead по TTL
