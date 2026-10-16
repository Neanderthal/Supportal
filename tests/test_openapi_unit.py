"""OpenAPI docs gating (SUPPORT_OPENAPI) on create_app + the reference product adapter's HMAC. Offline.

create_app is inspected for its docs/openapi URLs WITHOUT making requests, so the Telegram long-poller
lifespan never starts (no network). The dummy bot token is valid by format only.
"""
import hashlib
import hmac

from fastapi.testclient import TestClient

from supportal import settings as S
from supportal.app import create_app
from supportal.config import Config


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _cfg(monkeypatch):
    monkeypatch.setenv("SUPPORT_TG_BOT_TOKEN", "123456:abc")   # valid format; no network at construction
    monkeypatch.setenv("SUPPORT_DB_URL", "sqlite:///:memory:")
    return Config.from_env()


# ── docs gating ───────────────────────────────────────────────────────────────
def test_openapi_off_hides_docs(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_OPENAPI", raising=False)
    app = create_app(_cfg(monkeypatch))
    assert app.docs_url is None and app.redoc_url is None and app.openapi_url is None


def test_openapi_on_exposes_docs(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_OPENAPI", "on")
    app = create_app(_cfg(monkeypatch))
    assert app.docs_url == "/docs" and app.redoc_url == "/redoc" and app.openapi_url == "/openapi.json"


def test_openapi_schema_tags_the_routes(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_OPENAPI", "on")
    app = create_app(_cfg(monkeypatch))
    schema = app.openapi()
    assert schema["paths"]["/v1/widget/send"]["post"]["tags"] == ["widget"]
    assert schema["paths"]["/v1/messages"]["post"]["tags"] == ["product"]
    assert schema["paths"]["/health"]["get"]["tags"] == ["health"]


# ── reference product adapter (contour B) ───────────────────────────────────────
def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_adapter_callback_verifies_signature(monkeypatch):
    from examples import product_adapter as pa
    monkeypatch.setattr(pa, "CALLBACK_SECRET", "cb-secret")
    client = TestClient(pa.app)
    body = b'{"event":"message","user_ref":"u","ticket_id":"t","text":"hi","operator":"op"}'
    good = _sign(body, "cb-secret")
    r = client.post("/support/push", content=body,
                    headers={"Content-Type": "application/json", "X-Supportal-Signature": good})
    assert r.status_code == 200 and r.json() == {"ok": True, "delivered": 0}   # no live sessions → outbox retry
    bad = client.post("/support/push", content=body,
                      headers={"Content-Type": "application/json", "X-Supportal-Signature": "deadbeef"})
    assert bad.status_code == 401
