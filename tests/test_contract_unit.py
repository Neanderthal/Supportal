"""Оффлайн-юнит: стор (CRUD тикет↔топик) + HMAC-подпись контракта. Без сети/Telegram/aiogram."""
import os, tempfile

from supportal.models import IngressMessage
from supportal.relay import sign
from supportal.store import Store


def _store() -> Store:
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_"), "t.db")
    s = Store(f"sqlite:///{path}")
    s.bootstrap()
    return s


def test_ticket_lifecycle():
    s = _store()
    assert s.get_open_by_user("eng@x") is None
    t = s.create("eng@x", "Инженер")
    assert t.thread_id is None
    s.attach_thread(t.ticket_id, 42)
    reopened = s.get_open_by_user("eng@x")
    assert reopened is not None and reopened.thread_id == 42
    by_thread = s.get_by_thread(42)
    assert by_thread is not None and by_thread.ticket_id == t.ticket_id
    s.close(t.ticket_id)
    assert s.get_open_by_user("eng@x") is None


def test_signature_roundtrip():
    body = IngressMessage(user_ref="eng@x", text="нет давления в контуре").model_dump_json().encode()
    assert sign(body, "secret") == sign(body, "secret")
    assert sign(body, "secret") != sign(body, "other")
