"""SSE transport: the shared poll-payload builder + the gated /v1/widget/stream endpoint. Offline."""
import os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal import settings as S
from supportal.relay import Relay
from supportal.store import Store
from supportal.tokens import mint_widget_token
from supportal.widget_api import _build_poll, build_widget_router

_SECRET = "wsecret"


class _FakeTG:
    async def ensure_topic(self, title: str) -> int:
        return 1

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _client():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_sse_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    relay = Relay(store, _FakeTG(), "", "")
    app = FastAPI(); app.include_router(build_widget_router(relay, _SECRET))
    return TestClient(app), store


def _token(user="u"):
    return mint_widget_token(user, _SECRET)


# ── shared payload builder (used by /messages and /stream) ───────────────────
def test_build_poll():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_sse_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    store.add_message("t1", "u", "in", "U", "hi")
    store.add_message("t1", "u", "out", "Operator", "hello")
    rows = store.changes_since("u", 0)
    poll = _build_poll(rows, 0)
    assert [m.direction for m in poll.messages] == ["in", "out"]
    assert poll.cursor == rows[-1].seq
    assert _build_poll([], 7).cursor == 7 and _build_poll([], 7).messages == []


# ── endpoint gating ──────────────────────────────────────────────────────────
def test_stream_off_404(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_SSE", raising=False)
    client, _ = _client()
    assert client.get("/v1/widget/stream?token=" + _token()).status_code == 404


def test_stream_bad_token_401(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_SSE", "on")
    client, _ = _client()
    assert client.get("/v1/widget/stream?token=deadbeef").status_code == 401
    assert client.get("/v1/widget/stream").status_code == 401   # no token

# (The streaming happy-path — events pushed as the server detects changes — is covered by the demo
#  end-to-end rather than a unit test: an infinite SSE generator doesn't terminate cleanly under the
#  synchronous TestClient. The payload itself is `_build_poll`, tested above.)
