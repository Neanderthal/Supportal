"""Read receipts: store.mark_read + relay (reactions, flag gate) + the /v1/widget/read endpoint. Offline."""
import asyncio, os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal import settings as S
from supportal.relay import Relay
from supportal.store import Store
from supportal.tokens import mint_widget_token
from supportal.widget_api import build_widget_router

_SECRET = "wsecret"


class _RecTG:
    def __init__(self) -> None:
        self.reactions: list = []

    async def ensure_topic(self, title: str) -> int:
        return 1

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def react(self, tg_message_id: int, emoji: str = "👀") -> None:
        self.reactions.append((tg_message_id, emoji))

    async def delete_topic(self, thread_id: int) -> None:
        return None

    async def close(self) -> None:
        return None


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _store():
    st = Store(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='supportal_rcpt_'), 't.db')}")
    st.bootstrap()
    return st


def _seed(st):
    """A user message (in) + two operator replies (out) with Telegram ids."""
    st.add_message("t1", "u", "in", "U", "hi")
    st.add_message("t1", "u", "out", "Operator", "reply 1", tg_message_id=501)
    st.add_message("t1", "u", "out", "Operator", "reply 2", tg_message_id=502)
    return st.changes_since("u", 0)


# ── store.mark_read ──────────────────────────────────────────────────────────────
def test_mark_read_marks_out_up_to_seq_and_returns_tg_ids():
    st = _store()
    rows = _seed(st)
    out2_seq = rows[-1].seq
    tg = st.mark_read("u", out2_seq)
    assert sorted(tg) == [501, 502]                       # both operator messages' tg ids returned once
    after = {m.id: m for m in st.changes_since("u", 0)}
    assert all(m.read for m in after.values() if m.direction == "out")
    assert not any(m.read for m in after.values() if m.direction == "in")   # the user's own msg isn't a receipt


def test_mark_read_is_idempotent_and_respects_seq():
    st = _store()
    rows = _seed(st)
    first_out_seq = [m for m in rows if m.direction == "out"][0].seq
    assert st.mark_read("u", first_out_seq) == [501]      # only the first operator reply
    assert st.mark_read("u", first_out_seq) == []         # already read → nothing new
    # reply 2 (higher seq) is still unread
    assert [m.read for m in st.changes_since("u", 0) if m.direction == "out"] == [True, False]


# ── relay.mark_read (flag gate + reactions) ──────────────────────────────────────
def _relay():
    st = _store()
    tg = _RecTG()
    return Relay(st, tg, "", ""), st, tg


def test_relay_mark_read_off_is_noop(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_RECEIPTS", raising=False)
    relay, st, tg = _relay()
    _seed(st)
    assert asyncio.run(relay.mark_read("u", 9999)) == 0
    assert tg.reactions == []
    assert not any(m.read for m in st.changes_since("u", 0))     # nothing marked when off


def test_relay_mark_read_on_reacts(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_RECEIPTS", "on")
    relay, st, tg = _relay()
    _seed(st)
    n = asyncio.run(relay.mark_read("u", 9999))
    assert n == 2 and sorted(r[0] for r in tg.reactions) == [501, 502]
    assert all(r[1] == "👀" for r in tg.reactions)


# ── endpoint POST /v1/widget/read ─────────────────────────────────────────────────
def _client(relay):
    app = FastAPI(); app.include_router(build_widget_router(relay, _SECRET))
    return TestClient(app)


def _auth(user="u"):
    return {"Authorization": "Bearer " + mint_widget_token(user, _SECRET)}


def test_endpoint_requires_token(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_RECEIPTS", "on")
    relay, _, _ = _relay()
    assert _client(relay).post("/v1/widget/read", json={"seq": 5}).status_code == 401


def test_endpoint_on_marks_and_surfaces_read(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_RECEIPTS", "on")
    relay, st, _ = _relay()
    _seed(st)
    client = _client(relay)
    r = client.post("/v1/widget/read", json={"seq": 9999}, headers=_auth())
    assert r.status_code == 200 and r.json()["seen"] == 2
    msgs = client.get("/v1/widget/messages?since=0", headers=_auth()).json()["messages"]
    assert all(m["read"] for m in msgs if m["direction"] == "out")


def test_endpoint_off_marks_nothing(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_RECEIPTS", raising=False)
    relay, st, _ = _relay()
    _seed(st)
    client = _client(relay)
    assert client.post("/v1/widget/read", json={"seq": 9999}, headers=_auth()).json()["seen"] == 0
    msgs = client.get("/v1/widget/messages?since=0", headers=_auth()).json()["messages"]
    assert not any(m["read"] for m in msgs)
