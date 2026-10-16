"""Business-hours auto-away: sent once on first contact when out of hours, gated by the flag. Offline.

`is_within` is monkeypatched so the test does not depend on the wall clock / timezone.
"""
import asyncio, os, tempfile

from supportal import relay as relay_mod
from supportal import settings as S
from supportal.models import IngressMessage
from supportal.relay import Relay
from supportal.store import Store


class _RecTG:
    def __init__(self) -> None:
        self._n = 0
        self.posts: list = []

    async def ensure_topic(self, title: str) -> int:
        self._n += 1
        return self._n

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        self.posts.append((thread_id, text))

    async def delete_topic(self, thread_id: int) -> None:
        return None

    async def close(self) -> None:
        return None


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _relay():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_away_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    tg = _RecTG()
    return Relay(store, tg, "", ""), store, tg


def _ingress(relay, user, text):
    return asyncio.run(relay.ingress(IngressMessage(user_ref=user, text=text, display_name=user)))


def _away_rows(store, user):
    return [m for m in store.changes_since(user, 0) if m.kind == "away"]


def test_away_sent_when_out_of_hours(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_BUSINESS_HOURS", "on")
    monkeypatch.setenv("SUPPORT_AWAY_MESSAGE", "We are closed.")
    monkeypatch.setattr(relay_mod, "is_within", lambda *a, **k: False)   # force "out of hours"
    relay, store, tg = _relay()
    _ingress(relay, "u", "hola")
    rows = _away_rows(store, "u")
    assert len(rows) == 1 and rows[0].direction == "out" and rows[0].text == "We are closed."
    assert any("(auto-away)" in text for (_, text) in tg.posts)        # operator sees it in the topic


def test_away_not_sent_within_hours(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_BUSINESS_HOURS", "on")
    monkeypatch.setattr(relay_mod, "is_within", lambda *a, **k: True)    # within hours
    relay, store, _ = _relay()
    _ingress(relay, "u", "hola")
    assert _away_rows(store, "u") == []


def test_away_off_never_sent(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.delenv("SUPPORT_BUSINESS_HOURS", raising=False)
    monkeypatch.setattr(relay_mod, "is_within", lambda *a, **k: False)   # away, but the flag is off
    relay, store, _ = _relay()
    _ingress(relay, "u", "hola")
    assert _away_rows(store, "u") == []


def test_away_sent_once_per_ticket(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_BUSINESS_HOURS", "on")
    monkeypatch.setattr(relay_mod, "is_within", lambda *a, **k: False)
    relay, store, _ = _relay()
    _ingress(relay, "u", "first")
    _ingress(relay, "u", "second")                                       # same open ticket → no repeat
    assert len(_away_rows(store, "u")) == 1
