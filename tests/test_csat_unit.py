"""CSAT — close-notice prompt tagging, rating record + topic post, and the gated endpoint. Offline."""
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
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_csat_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    tg = _RecTG()
    return Relay(store, tg, "", ""), store, tg


def _open_and_close(relay, store):
    t = store.create("u", "U")
    store.attach_thread(t.ticket_id, 1)
    asyncio.run(relay.on_operator_command(1, "/close", "op"))
    return store.changes_since("u", 0)[-1]


# ── store: last ticket + kind roundtrip ────────────────────────────────────────
def test_get_last_by_user_regardless_of_status():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_csat_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    assert store.get_last_by_user("u") is None
    t1 = store.create("u", "U")
    store.close(t1.ticket_id)                         # closed, but still the most recent
    got = store.get_last_by_user("u")
    assert got is not None and got.ticket_id == t1.ticket_id and got.status == "closed"


def test_add_message_kind_roundtrips():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_csat_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    t = store.create("u", "U")
    store.add_message(t.ticket_id, "u", "in", "U", "⭐ 5/5", kind="csat")
    assert store.changes_since("u", 0)[-1].kind == "csat"


# ── relay: close-notice tagging ────────────────────────────────────────────────
def test_close_notice_tagged_when_on(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_CSAT", "on")
    relay, store, _ = _relay()
    assert _open_and_close(relay, store).kind == "csat_request"


def test_close_notice_plain_when_off(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_CSAT", raising=False)
    relay, store, _ = _relay()
    assert _open_and_close(relay, store).kind == "msg"


# ── relay: submit_csat ─────────────────────────────────────────────────────────
def test_submit_csat_off_returns_none(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_CSAT", raising=False)
    relay, store, _ = _relay()
    store.attach_thread(store.create("u", "U").ticket_id, 1)
    assert asyncio.run(relay.submit_csat("u", 5)) is None


def test_submit_csat_no_ticket_returns_none(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_CSAT", "on")
    relay, _, _ = _relay()
    assert asyncio.run(relay.submit_csat("ghost", 4)) is None


def test_submit_csat_records_row_and_posts_topic(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_CSAT", "on")
    relay, store, tg = _relay()
    store.attach_thread(store.create("u", "U").ticket_id, 7)
    mid = asyncio.run(relay.submit_csat("u", 4, "great help"))
    assert mid and mid > 0
    row = store.changes_since("u", 0)[-1]
    assert row.kind == "csat" and row.direction == "in" and "4/5" in row.text and "great help" in row.text
    posted = [text for (_, text) in tg.posts if "CSAT" in text]
    assert posted and "4/5" in posted[0]             # operator sees the score in the topic


def test_submit_csat_clamps_score(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_CSAT", "on")
    relay, store, _ = _relay()
    store.attach_thread(store.create("u", "U").ticket_id, 1)
    asyncio.run(relay.submit_csat("u", 99))
    assert "5/5" in store.changes_since("u", 0)[-1].text   # clamped to the 1..5 range


# ── endpoint ───────────────────────────────────────────────────────────────────
def _client(relay):
    app = FastAPI(); app.include_router(build_widget_router(relay, _SECRET))
    return TestClient(app)


def _auth(user="u"):
    return {"Authorization": "Bearer " + mint_widget_token(user, _SECRET)}


def test_endpoint_off_404(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_CSAT", raising=False)
    relay, store, _ = _relay()
    store.attach_thread(store.create("u", "U").ticket_id, 1)
    r = _client(relay).post("/v1/widget/csat", json={"score": 5}, headers=_auth())
    assert r.status_code == 404


def test_endpoint_happy_lands_in_feed(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_CSAT", "on")
    relay, store, _ = _relay()
    store.attach_thread(store.create("u", "U").ticket_id, 1)
    client = _client(relay)
    r = client.post("/v1/widget/csat", json={"score": 5}, headers=_auth())
    assert r.status_code == 200 and r.json()["ok"]
    msgs = client.get("/v1/widget/messages?since=0", headers=_auth()).json()["messages"]
    csat_rows = [m for m in msgs if m["kind"] == "csat"]
    assert csat_rows and csat_rows[0]["direction"] == "in" and "5/5" in csat_rows[0]["text"]


def test_endpoint_requires_token(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_CSAT", "on")
    relay, _, _ = _relay()
    assert _client(relay).post("/v1/widget/csat", json={"score": 5}).status_code == 401


def test_endpoint_score_out_of_range_422(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_CSAT", "on")
    relay, store, _ = _relay()
    store.attach_thread(store.create("u", "U").ticket_id, 1)
    client = _client(relay)
    assert client.post("/v1/widget/csat", json={"score": 0}, headers=_auth()).status_code == 422
    assert client.post("/v1/widget/csat", json={"score": 6}, headers=_auth()).status_code == 422


def test_endpoint_no_ticket_404(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_CSAT", "on")
    relay, _, _ = _relay()
    r = _client(relay).post("/v1/widget/csat", json={"score": 5}, headers=_auth("ghost"))
    assert r.status_code == 404
