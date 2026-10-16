"""AI auto-answer: KB loading, the LLM call (httpx mocked), and the relay auto-answer path. Offline."""
import asyncio, os, tempfile

import httpx

from supportal import ai as ai_mod
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

    async def close(self) -> None:
        return None


class _FakeResp:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    def __init__(self, content: str) -> None:
        self._content = content

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        return _FakeResp(self._content)


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _kb(tmp_path, text="# Hours\nWe are open Mon-Fri 9-18.") -> str:
    d = tmp_path / "kb"; d.mkdir()
    (d / "hours.md").write_text(text, encoding="utf-8")
    return str(d)


def _configure_llm(monkeypatch, tmp_path, content):
    monkeypatch.setenv("SUPPORT_AI_KB_DIR", _kb(tmp_path))
    monkeypatch.setenv("SUPPORT_AI_API_BASE", "https://llm.example/v1")
    monkeypatch.setenv("SUPPORT_AI_API_KEY", "k")
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(content))


# ── KB loading ───────────────────────────────────────────────────────────────
def test_load_kb_reads_caps_and_missing():
    assert ai_mod.load_kb("/no/such/dir", 8000) == ""
    with tempfile.TemporaryDirectory() as d:
        assert ai_mod.load_kb(d, 8000) == ""                    # dir exists but no .md
        open(os.path.join(d, "a.md"), "w").write("Hello KB")
        open(os.path.join(d, "b.md"), "w").write("Second doc")
        kb = ai_mod.load_kb(d, 8000)
        assert "## a.md" in kb and "Hello KB" in kb and "Second doc" in kb
        assert len(ai_mod.load_kb(d, 5)) == 5                    # capped


# ── auto_answer (LLM mocked) ─────────────────────────────────────────────────
def test_auto_answer_returns_text(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_AI", "on")
    _configure_llm(monkeypatch, tmp_path, "We're open Mon-Fri 9-18.")
    assert asyncio.run(ai_mod.auto_answer("When are you open?")) == "We're open Mon-Fri 9-18."


def test_auto_answer_handoff_is_none(tmp_path, monkeypatch):
    _force_env_only(tmp_path); _configure_llm(monkeypatch, tmp_path, "HANDOFF")
    assert asyncio.run(ai_mod.auto_answer("Something off-topic")) is None


def test_auto_answer_no_kb_is_none(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_AI_KB_DIR", str(tmp_path / "empty"))   # missing dir
    monkeypatch.setenv("SUPPORT_AI_API_BASE", "x"); monkeypatch.setenv("SUPPORT_AI_API_KEY", "k")
    assert asyncio.run(ai_mod.auto_answer("hi")) is None


def test_auto_answer_no_credentials_is_none(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_AI_KB_DIR", _kb(tmp_path))
    monkeypatch.delenv("SUPPORT_AI_API_BASE", raising=False)
    monkeypatch.delenv("SUPPORT_AI_API_KEY", raising=False)
    assert asyncio.run(ai_mod.auto_answer("hi")) is None


# ── relay auto-answer path ────────────────────────────────────────────────────
def _relay():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_ai_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    tg = _RecTG()
    return Relay(store, tg, "", ""), store, tg


def _ai_rows(store):
    return [m for m in store.changes_since("u", 0) if m.kind == "ai"]


def test_relay_ai_answer_records_and_posts(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_AI", "on")

    async def _fake(q):
        return "Here is the answer."
    monkeypatch.setattr(ai_mod, "auto_answer", _fake)
    relay, store, tg = _relay()
    t = store.create("u", "U"); store.attach_thread(t.ticket_id, 5); t.thread_id = 5
    asyncio.run(relay._ai_answer(t, "a question"))
    rows = _ai_rows(store)
    assert len(rows) == 1 and rows[0].direction == "out" and rows[0].author == "Assistant"
    assert rows[0].text == "Here is the answer."
    assert any("(auto-answer)" in text for (_, text) in tg.posts)


def test_relay_ai_handoff_records_nothing(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_AI", "on")

    async def _fake(q):
        return None
    monkeypatch.setattr(ai_mod, "auto_answer", _fake)
    relay, store, _ = _relay()
    t = store.create("u", "U"); store.attach_thread(t.ticket_id, 5); t.thread_id = 5
    asyncio.run(relay._ai_answer(t, "a question"))
    assert _ai_rows(store) == []


def test_ingress_spawns_ai_when_on(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_AI", "on")

    async def _fake(q):
        return "Auto reply from KB."
    monkeypatch.setattr(ai_mod, "auto_answer", _fake)
    relay, store, _ = _relay()

    async def scenario():
        await relay.ingress(IngressMessage(user_ref="u", text="q", display_name="U"))
        await asyncio.gather(*list(relay._bg_tasks))   # drain the background AI task deterministically
    asyncio.run(scenario())
    assert [m.text for m in _ai_rows(store)] == ["Auto reply from KB."]


def test_ingress_no_ai_when_off(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_AI", raising=False)

    async def _fake(q):
        return "should not be called"
    monkeypatch.setattr(ai_mod, "auto_answer", _fake)
    relay, store, _ = _relay()

    async def scenario():
        await relay.ingress(IngressMessage(user_ref="u", text="q", display_name="U"))
        await asyncio.gather(*list(relay._bg_tasks))
    asyncio.run(scenario())
    assert _ai_rows(store) == []
