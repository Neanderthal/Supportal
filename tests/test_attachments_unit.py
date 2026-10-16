"""attachments — store roundtrip + upload/serve endpoints (flag-gated, ownership, size cap). Offline."""
import os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal import settings as S
from supportal.relay import Relay
from supportal.store import Store
from supportal.tokens import mint_widget_token
from supportal.widget_api import _content_disposition, build_widget_router

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


def test_upload_rejects_oversize_before_buffering(tmp_path, monkeypatch):
    # arch review #4: a declared Content-Length over the cap is rejected 413 before the body is buffered.
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_ATTACHMENTS", "on")
    monkeypatch.setenv("SUPPORT_ATTACHMENTS_MAX_MB", "1")
    client = _client(); token = mint_widget_token("u1", _SECRET)
    hdr = {"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream"}
    over = client.post("/v1/widget/upload?name=big.bin", content=b"x" * (1024 * 1024 + 1), headers=hdr)
    assert over.status_code == 413
    within = client.post("/v1/widget/upload?name=ok.bin", content=b"y" * 1024, headers=hdr)
    assert within.status_code == 200


def test_upload_streaming_cap_without_content_length(tmp_path, monkeypatch):
    # No Content-Length (chunked body) → the streaming hard-cap still trips 413 (can't lie past the guard).
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_ATTACHMENTS", "on")
    monkeypatch.setenv("SUPPORT_ATTACHMENTS_MAX_MB", "1")
    client = _client(); token = mint_widget_token("u1", _SECRET)

    def _chunked():
        for _ in range(3):
            yield b"z" * (512 * 1024)                          # 1.5 MiB total, streamed, no Content-Length
    r = client.post("/v1/widget/upload?name=stream.bin", content=_chunked(),
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream"})
    assert r.status_code == 413


# ── endpoints (flag off → feature absent) ────────────────────────────────────
def test_flag_off_upload_and_serve_404(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_ATTACHMENTS", raising=False)
    client = _client(); token = mint_widget_token("u1", _SECRET)
    assert _upload(client, token).status_code == 404
    assert client.get("/v1/widget/attachment/1", headers=_auth(token)).status_code == 404


# ── Content-Disposition filename sanitization (arch review #5) ─────────────────
def test_content_disposition_helper_strips_injection():
    cd = _content_disposition('a"b\r\nSet-Cookie: x.pdf')
    assert "\r" not in cd and "\n" not in cd                  # no header injection
    assert cd.count('"') == 2                                 # only the two delimiters; inner quote stripped
    assert cd.startswith('attachment; filename="')
    assert "filename*=UTF-8''" in cd                          # RFC 5987 form present
    assert 'filename="file"' in _content_disposition("")      # empty → "file"


def test_download_filename_sanitized_no_header_injection(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_ATTACHMENTS", "on")
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_att_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    # a NON-image (so Content-Disposition is set) with a hostile filename: quote + CRLF injection attempt
    aid = store.add_attachment("u1", "in", 'in"voice\r\nSet-Cookie: pwn.pdf', "application/pdf", b"%PDF fake")
    relay = Relay(store, _FakeTG(), "", "")
    app = FastAPI(); app.include_router(build_widget_router(relay, _SECRET))
    g = TestClient(app).get("/v1/widget/attachment/" + str(aid), headers=_auth(mint_widget_token("u1", _SECRET)))
    assert g.status_code == 200
    cd = g.headers["content-disposition"]
    assert "\r" not in cd and "\n" not in cd and cd.count('"') == 2
    assert "filename*=UTF-8''" in cd


def test_attachment_serve_nosniff_and_svg_forced_download(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_ATTACHMENTS", "on")
    path = os.path.join(tempfile.mkdtemp(prefix="supportal_att_"), "t.db")
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    png = store.add_attachment("u1", "in", "a.png", "image/png", _PNG)
    svg = store.add_attachment("u1", "in", "x.svg", "image/svg+xml", b"<svg onload=alert(1)/>")
    relay = Relay(store, _FakeTG(), "", "")
    app = FastAPI(); app.include_router(build_widget_router(relay, _SECRET))
    client = TestClient(app); hdr = _auth(mint_widget_token("u1", _SECRET))
    # raster image → inline, but always nosniff
    rp = client.get("/v1/widget/attachment/" + str(png), headers=hdr)
    assert rp.status_code == 200 and rp.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in rp.headers
    # svg → forced download (never inline) + nosniff
    rs = client.get("/v1/widget/attachment/" + str(svg), headers=hdr)
    assert rs.status_code == 200 and rs.headers["x-content-type-options"] == "nosniff"
    assert rs.headers["content-disposition"].startswith("attachment")
