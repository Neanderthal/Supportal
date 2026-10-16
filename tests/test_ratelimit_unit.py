"""ratelimit.py — sliding window (deterministic via now) + 429 on /v1/widget/send behind a flag."""
import os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal import ratelimit
from supportal import settings as S
from supportal.ratelimit import SlidingWindowLimiter
from supportal.relay import Relay
from supportal.store import Store
from supportal.tokens import mint_widget_token
from supportal.widget_api import build_widget_router

_SECRET = "wsecret"


# ── unit: window ──────────────────────────────────────────────────────────────
def test_window_allows_then_blocks():
    lim = SlidingWindowLimiter()
    assert lim.allow("u", 3, 60, now=100.0)
    assert lim.allow("u", 3, 60, now=101.0)
    assert lim.allow("u", 3, 60, now=102.0)
    assert not lim.allow("u", 3, 60, now=103.0)      # 4th in the window → block
    assert lim.allow("u", 3, 60, now=170.0)          # window moved on → allowed again


def test_window_eviction_and_key_isolation():
    lim = SlidingWindowLimiter()
    assert lim.allow("a", 1, 10, now=0.0)
    assert not lim.allow("a", 1, 10, now=5.0)
    assert lim.allow("a", 1, 10, now=11.0)           # old hit evicted from the window
    assert lim.allow("b", 1, 10, now=5.0)             # a different key is independent


def test_nonpositive_max_never_blocks():
    lim = SlidingWindowLimiter()
    assert lim.allow("u", 0, 60, now=1.0)
    assert lim.allow("u", -1, 60, now=1.0)


# ── integration: the flag gates 429 on the endpoint ──────────────────────────
class _FakeTG:
    def __init__(self) -> None:
        self._n = 0

    async def ensure_topic(self, title: str) -> int:
        self._n += 1
        return self._n

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def close(self) -> None:
        return None


def _client() -> TestClient:
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_rl_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    relay = Relay(store, _FakeTG(), "", "")
    app = FastAPI(); app.include_router(build_widget_router(relay, _SECRET))
    return TestClient(app)


def _send(client: TestClient, token: str, text: str = "hi"):
    return client.post("/v1/widget/send", json={"text": text},
                       headers={"Authorization": "Bearer " + token})


def _force_env_only(tmp_path):
    # no settings.yaml → values come from env (fail-soft)
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def test_flag_off_no_limit(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.delenv("SUPPORT_RATE_LIMIT", raising=False)
    ratelimit.limiter.reset()
    client = _client(); token = mint_widget_token("u1", _SECRET)
    for _ in range(5):
        assert _send(client, token).status_code == 200


def test_flag_on_blocks_after_max(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_RATE_LIMIT", "on")
    monkeypatch.setenv("SUPPORT_RATE_LIMIT_MAX", "2")
    monkeypatch.setenv("SUPPORT_RATE_LIMIT_WINDOW", "60")
    ratelimit.limiter.reset()
    client = _client(); token = mint_widget_token("u2", _SECRET)
    assert _send(client, token).status_code == 200
    assert _send(client, token).status_code == 200
    assert _send(client, token).status_code == 429       # 3rd in the window → block
    other = mint_widget_token("u3", _SECRET)
    assert _send(client, other).status_code == 200        # a different user is unaffected
