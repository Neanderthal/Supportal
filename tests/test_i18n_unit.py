"""i18n — the one server-generated user-facing line (ticket-closed notice) is a settings string.

(The widget's own UI copy is localized client-side via /support/config `strings`; that path is
vanilla JS, covered by the demo smoke, not a unit test.)
"""
import asyncio, os, tempfile

from supportal import settings as S
from supportal.relay import Relay
from supportal.store import Store


class _FakeTG:
    async def ensure_topic(self, title: str) -> int:
        return 1

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def delete_topic(self, thread_id: int) -> None:
        return None

    async def close(self) -> None:
        return None


def _relay():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_i18n_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    return Relay(store, _FakeTG(), "", ""), store


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _close(relay, store):
    t = store.create("u", "U")
    store.attach_thread(t.ticket_id, 1)
    asyncio.run(relay.on_operator_command(1, "/close", "op"))
    return store.changes_since("u", 0)[-1].text


def test_closed_notice_default(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.delenv("SUPPORT_CLOSED_NOTICE", raising=False)
    relay, store = _relay()
    assert _close(relay, store) == "This ticket was closed by the operator."


def test_closed_notice_override(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_CLOSED_NOTICE", "Ticket cerrado por el operador.")
    relay, store = _relay()
    assert _close(relay, store) == "Ticket cerrado por el operador."
