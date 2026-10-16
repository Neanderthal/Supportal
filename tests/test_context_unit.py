"""visitor context — block formatting/caps + posted once on topic creation, gated by the flag. Offline."""
import asyncio, os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal import settings as S
from supportal.models import IngressMessage
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

    async def send_media(self, thread_id, data, filename, mime, caption="") -> None:
        return None

    async def delete_topic(self, thread_id: int) -> None:
        return None

    async def close(self) -> None:
        return None


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _relay():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_ctx_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    tg = _RecTG()
    return Relay(store, tg, "", ""), tg


def _ctx_posts(tg):
    return [text for (_, text) in tg.posts if text.startswith("🧭")]


def _ingress(relay, user, text, context=None):
    return asyncio.run(relay.ingress(
        IngressMessage(user_ref=user, text=text, display_name=user, context=context or {})))


# ── block formatting ─────────────────────────────────────────────────────────
def test_block_off_returns_none(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_VISITOR_CONTEXT", raising=False)
    relay, _ = _relay()
    assert relay._context_block({"url": "x"}) is None


def test_block_on_formats_and_sanitizes(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_VISITOR_CONTEXT", "on")
    relay, _ = _relay()
    b = relay._context_block({"url": "https://x/y", "note": "a\nb", "blank": "  "})
    assert b.startswith("🧭")
    assert "url: https://x/y" in b
    assert "note: a b" in b          # newline stripped
    assert "blank" not in b          # empty value skipped
    assert relay._context_block({}) is None


# ── posted once on topic creation ─────────────────────────────────────────────
def test_context_posted_once(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_VISITOR_CONTEXT", "on")
    relay, tg = _relay()
    _ingress(relay, "u", "hi", {"url": "https://a"})
    assert len(_ctx_posts(tg)) == 1               # posted when the topic is created
    _ingress(relay, "u", "again", {"url": "https://a"})
    assert len(_ctx_posts(tg)) == 1               # NOT repeated on the existing topic


def test_context_off_not_posted(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_VISITOR_CONTEXT", raising=False)
    relay, tg = _relay()
    _ingress(relay, "u", "hi", {"url": "https://a"})
    assert _ctx_posts(tg) == []


# ── widget endpoint forwards context ──────────────────────────────────────────
def test_widget_send_forwards_context(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_VISITOR_CONTEXT", "on")
    relay, tg = _relay()
    app = FastAPI(); app.include_router(build_widget_router(relay, _SECRET))
    client = TestClient(app); token = mint_widget_token("u", _SECRET)
    r = client.post("/v1/widget/send", json={"text": "hi", "context": {"url": "https://p/x", "plan": "Pro"}},
                    headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200
    posts = _ctx_posts(tg)
    assert posts and "plan: Pro" in posts[0] and "url: https://p/x" in posts[0]
