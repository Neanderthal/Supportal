"""Canned responses: settings.get_dict + relay "/c <key>" expansion (flag-gated). Offline."""
import asyncio, os, tempfile

from supportal import settings as S
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


def _relay_with_ticket():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_canned_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    tg = _RecTG()
    relay = Relay(store, tg, "", "")
    t = store.create("u", "U")
    store.attach_thread(t.ticket_id, 7)
    return relay, store, tg


def _out_rows(store):
    return [m for m in store.changes_since("u", 0) if m.direction == "out"]


def _cmd(relay, command):
    asyncio.run(relay.on_operator_command(7, command, "op"))


# ── settings.get_dict ──────────────────────────────────────────────────────────
def test_get_dict_from_env_string(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_CANNED_REPLIES", "a=hi there; b = bye ;broken;c=x=y")
    d = S.get_dict("SUPPORT_CANNED_REPLIES")
    assert d == {"a": "hi there", "b": "bye", "c": "x=y"}      # trimmed, no-"=" skipped, split once


def test_get_dict_absent_and_default(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.delenv("SUPPORT_CANNED_REPLIES", raising=False)
    assert S.get_dict("SUPPORT_CANNED_REPLIES") == {}
    assert S.get_dict("SUPPORT_CANNED_REPLIES", {"x": "y"}) == {"x": "y"}


# ── relay "/c <key>" ─────────────────────────────────────────────────────────────
def test_canned_known_key_sends_reply(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_CANNED", "on")
    monkeypatch.setenv("SUPPORT_CANNED_REPLIES", "saludo=Hola!;hours=9-18")
    relay, store, tg = _relay_with_ticket()
    _cmd(relay, "/c saludo")
    out = _out_rows(store)
    assert len(out) == 1 and out[0].text == "Hola!" and out[0].author == "Operator"
    assert any("(canned: saludo)" in text for (_, text) in tg.posts)   # topic confirmation for the operator


def test_canned_unknown_key_lists_and_sends_nothing(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_CANNED", "on")
    monkeypatch.setenv("SUPPORT_CANNED_REPLIES", "saludo=Hola!;hours=9-18")
    relay, store, tg = _relay_with_ticket()
    _cmd(relay, "/c nope")
    assert _out_rows(store) == []                                       # nothing sent to the user
    listed = [text for (_, text) in tg.posts if "Canned replies" in text]
    assert listed and "hours" in listed[0] and "saludo" in listed[0]


def test_canned_empty_key_lists(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_CANNED", "on")
    monkeypatch.setenv("SUPPORT_CANNED_REPLIES", "saludo=Hola!")
    relay, store, tg = _relay_with_ticket()
    _cmd(relay, "/c")
    assert _out_rows(store) == []
    assert any("Canned replies" in text for (_, text) in tg.posts)


def test_canned_off_sends_nothing(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.delenv("SUPPORT_CANNED", raising=False)
    monkeypatch.setenv("SUPPORT_CANNED_REPLIES", "saludo=Hola!")
    relay, store, tg = _relay_with_ticket()
    _cmd(relay, "/c saludo")
    assert _out_rows(store) == []
    assert any("disabled" in text for (_, text) in tg.posts)


def test_close_and_clear_still_work_with_c_prefix_check(tmp_path, monkeypatch):
    # "/close" and "/clear" must NOT be mistaken for the "/c" canned command.
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_CANNED", "on")
    relay, store, _ = _relay_with_ticket()
    _cmd(relay, "/close")
    assert store.get_open_by_user("u") is None                         # ticket actually closed, not treated as canned
