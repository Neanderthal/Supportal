"""app.create_app wiring + lifespan (arch review: app.py was the weakest-covered, concurrency-critical glue).

Offline: TelegramOps is constructed with a format-valid dummy token (no network until a bot method runs);
start_polling / close / Relay.retry_outbox_once are monkeypatched so the lifespan starts, runs its background
tasks in the TestClient's loop, and shuts them down without touching Telegram.
"""
import asyncio
import os
import tempfile
import threading
import time

from fastapi.testclient import TestClient

from supportal.app import create_app
from supportal.config import Config
from supportal.relay import Relay
from supportal.telegram_ops import TelegramOps

_DUMMY_TOKEN = "123456:abc"      # valid by format; no network until a bot method is called
_GROUP_ID = -1001234567890


def _cfg(**over) -> Config:
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_app_"), "t.db")
    base = dict(
        tg_bot_token=_DUMMY_TOKEN, tg_group_id=_GROUP_ID, tg_proxy="",
        product_callback_url="", callback_secret="", ingress_secret="s",
        widget_secret="w", allowed_origin="", db_url=f"sqlite:///{path}", http_port=8500,
        outbox_retry_interval=0, outbox_ttl=86400, outbox_max_attempts=12,
        outbox_backoff_base=30, outbox_backoff_cap=1800,
    )
    base.update(over)
    return Config(**base)


def _wait(cond, timeout=2.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_create_app_wires_routers():
    # Behavioral check (this FastAPI wraps included routers, so introspecting app.routes paths is unreliable):
    # health answers, ingress + widget routes exist and are auth-gated.
    client = TestClient(create_app(_cfg()))
    assert client.get("/health").json() == {"status": "ok", "service": "supportal"}
    assert client.post("/v1/messages", content=b"{}").status_code == 401        # ingress HMAC-gated
    assert client.get("/v1/widget/messages").status_code == 401                  # widget Bearer-gated
    assert client.post("/v1/widget/send", json={"text": "hi"}).status_code == 401


def test_create_app_no_cors_without_origin():
    app = create_app(_cfg())
    assert not any("CORSMiddleware" in str(m) for m in app.user_middleware)


def test_create_app_adds_cors_when_origin_set():
    app = create_app(_cfg(allowed_origin="https://product.example"))
    assert any("CORSMiddleware" in str(m) for m in app.user_middleware)


def test_lifespan_starts_tasks_logs_and_closes(monkeypatch, caplog):
    started = threading.Event()
    closed = threading.Event()

    async def _block(self, *a):          # long-poller: block until cancelled at shutdown
        started.set()
        await asyncio.Event().wait()

    async def _close(self):
        closed.set()

    monkeypatch.setattr(TelegramOps, "start_polling", _block)
    monkeypatch.setattr(TelegramOps, "close", _close)

    app = create_app(_cfg(outbox_retry_interval=3600))   # outbox loop just sleeps; we only check start/stop
    with caplog.at_level("INFO"):
        with TestClient(app):
            assert _wait(started.is_set)                  # background long-poller task actually ran
    assert closed.is_set()                                # shutdown cancelled tasks and closed the bot session
    assert "SINGLE-PROCESS" in caplog.text               # the loud single-process startup line


def test_outbox_loop_invokes_retry(monkeypatch):
    fired = threading.Event()

    async def _block(self, *a):
        await asyncio.Event().wait()

    async def _close(self):
        return None

    async def _retry(self):
        fired.set()
        return 0

    monkeypatch.setattr(TelegramOps, "start_polling", _block)
    monkeypatch.setattr(TelegramOps, "close", _close)
    monkeypatch.setattr(Relay, "retry_outbox_once", _retry)

    with TestClient(create_app(_cfg(outbox_retry_interval=0))):
        assert _wait(fired.is_set)                        # the periodic outbox loop called retry_outbox_once


def test_outbox_loop_survives_a_failing_retry(monkeypatch):
    # A crash in a retry pass must be logged and the loop must keep going (app._outbox_loop except branch) —
    # so retry is called again (>1) rather than the task dying on the first exception.
    calls = []

    async def _block(self, *a):
        await asyncio.Event().wait()

    async def _close(self):
        return None

    async def _boom(self):
        calls.append(1)
        raise RuntimeError("delivery pass blew up")

    monkeypatch.setattr(TelegramOps, "start_polling", _block)
    monkeypatch.setattr(TelegramOps, "close", _close)
    monkeypatch.setattr(Relay, "retry_outbox_once", _boom)

    with TestClient(create_app(_cfg(outbox_retry_interval=0))):
        assert _wait(lambda: len(calls) >= 2)             # loop survived the first exception and retried
