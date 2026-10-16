"""attachments — store roundtrip + upload/serve endpoints (flag-gated, ownership, size cap). Offline."""
import os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal import settings as S
from supportal.relay import Relay
from supportal.store import Store
from supportal.tokens import mint_widget_token
from supportal.widget_api import build_widget_router

_SECRET = "wsecret"
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32   # fake image bytes (only the prefix matters here)


class _FakeTG:
    def __init__(self) -> None:
        self._n = 0
        self.sent_media: list = []

    async def ensure_topic(self, title: str) -> int:
        self._n += 1
        return self._n

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def send_media(self, thread_id: int, data: bytes, filename: str, mime: str, caption: str = "") -> None:
        self.sent_media.append((thread_id, filename, mime, len(data)))

    async def close(self) -> None:
        return None


def _store() -> Store:
    st = Store(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='supportal_att_'), 't.db')}")
    st.bootstrap()
    return st


def _client():
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_att_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    relay = Relay(store, _FakeTG(), "", "")
    app = FastAPI(); app.include_router(build_widget_router(relay, _SECRET))
    return TestClient(app)


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _auth(token):
    return {"Authorization": "Bearer " + token}


def _upload(client, token, data=_PNG, name="shot.png", ctype="image/png"):
    return client.post("/v1/widget/upload?name=" + name, content=data,
                       headers={"Authorization": "Bearer " + token, "Content-Type": ctype})


# ── store ────────────────────────────────────────────────────────────────────
def test_store_attachment_roundtrip():
    st = _store()
    aid = st.add_attachment("u1", "in", "a.png", "image/png", b"xyz")
    att = st.get_attachment(aid)
    assert att is not None
    assert att.data == b"xyz" and att.mime == "image/png" and att.user_ref == "u1" and att.size == 3
    assert st.get_attachment(999999) is None


def test_store_message_carries_attachment_meta():
    st = _store()
    t = st.create("u1", "U1")
    aid = st.add_attachment("u1", "in", "a.png", "image/png", b"xyz")
    st.add_message(t.ticket_id, "u1", "in", "U1", "", attachment_id=aid)
    rows = st.changes_since("u1", 0)
    assert rows[-1].attachment_id == aid
    assert rows[-1].attachment_name == "a.png" and rows[-1].attachment_mime == "image/png"


# ── endpoints (flag on) ──────────────────────────────────────────────────────
def test_upload_lands_in_feed_and_serves(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_ATTACHMENTS", "on")
    client = _client(); token = mint_widget_token("u1", _SECRET)
    r = _upload(client, token)
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] and j["attachment_id"] > 0
    # the feed row carries the attachment metadata
    msgs = client.get("/v1/widget/messages?since=0", headers=_auth(token)).json()["messages"]
    att_rows = [m for m in msgs if m["attachment_id"]]
    assert att_rows and att_rows[0]["attachment_mime"] == "image/png" and att_rows[0]["direction"] == "in"
    # the bytes are served back with the right content-type
    g = client.get("/v1/widget/attachment/" + str(j["attachment_id"]), headers=_auth(token))
    assert g.status_code == 200 and g.content == _PNG and g.headers["content-type"].startswith("image/png")


def test_attachment_ownership_enforced(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_ATTACHMENTS", "on")
    client = _client()
    aid = _upload(client, mint_widget_token("u1", _SECRET)).json()["attachment_id"]
    # another user must not read someone else's attachment
    g = client.get("/v1/widget/attachment/" + str(aid), headers=_auth(mint_widget_token("u2", _SECRET)))
    assert g.status_code == 404


def test_size_cap_rejects(tmp_path, monkeypatch):
    _force_env_only(tmp_path)
    monkeypatch.setenv("SUPPORT_ATTACHMENTS", "on")
    monkeypatch.setenv("SUPPORT_ATTACHMENTS_MAX_MB", "0")     # cap 0 → any non-empty body is too large
    client = _client(); token = mint_widget_token("u1", _SECRET)
    assert _upload(client, token).status_code == 413


# ── endpoints (flag off → feature absent) ────────────────────────────────────
def test_flag_off_upload_and_serve_404(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_ATTACHMENTS", raising=False)
    client = _client(); token = mint_widget_token("u1", _SECRET)
    assert _upload(client, token).status_code == 404
    assert client.get("/v1/widget/attachment/1", headers=_auth(token)).status_code == 404
