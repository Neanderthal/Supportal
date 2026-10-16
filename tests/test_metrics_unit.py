"""Metrics: store counts, the Prometheus renderer, and the gated /metrics endpoint. Offline."""
import os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal import metrics as M
from supportal import settings as S
from supportal.api import build_router
from supportal.relay import Relay
from supportal.store import Store


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


def _store():
    st = Store(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='supportal_metrics_'), 't.db')}")
    st.bootstrap()
    return st


def _seed(st):
    t = st.create("u", "U")
    st.attach_thread(t.ticket_id, 1)
    st.add_message(t.ticket_id, "u", "in", "U", "hi")
    st.add_message(t.ticket_id, "u", "out", "Operator", "hello")
    aid = st.add_attachment("u", "in", "a.png", "image/png", b"x")
    st.add_message(t.ticket_id, "u", "in", "U", "", attachment_id=aid)
    t2 = st.create("v", "V")
    st.close(t2.ticket_id)
    return st


# ── store.metrics ────────────────────────────────────────────────────────────
def test_store_metrics_counts():
    st = _seed(_store())
    m = st.metrics()
    assert m["tickets_open"] == 1 and m["tickets_closed"] == 1
    assert m["messages_in"] == 2 and m["messages_out"] == 1 and m["attachments"] == 1
    assert m["outbox_pending"] == 0


# ── renderer ─────────────────────────────────────────────────────────────────
def test_render_prometheus_format():
    text = M.render({"tickets_open": 3, "tickets_closed": 1, "messages_in": 12, "messages_out": 8,
                     "attachments": 2, "outbox_pending": 0}, version="9.9.9")
    assert "# TYPE supportal_tickets gauge" in text
    assert 'supportal_tickets{status="open"} 3' in text
    assert 'supportal_messages{direction="out"} 8' in text
    assert "supportal_attachments 2" in text
    assert 'supportal_build_info{version="9.9.9"} 1' in text
    assert text.endswith("\n")


# ── endpoint ─────────────────────────────────────────────────────────────────
def _client():
    st = _seed(_store())
    app = FastAPI(); app.include_router(build_router(Relay(st, _FakeTG(), "", ""), "sec"))
    return TestClient(app)


def test_endpoint_off_404(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_METRICS", raising=False)
    assert _client().get("/metrics").status_code == 404


def test_endpoint_on_renders(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_METRICS", "on")
    r = _client().get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert 'supportal_tickets{status="open"} 1' in body and 'supportal_messages{direction="in"} 2' in body
