"""GDPR export/erase: store methods + relay gating + the HMAC-signed admin endpoints. Offline."""
import asyncio, base64, json, os, tempfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from supportal import settings as S
from supportal.api import build_router
from supportal.relay import Relay, sign
from supportal.store import Store

_SECRET = "ingress-secret"


class _RecTG:
    def __init__(self) -> None:
        self.deleted_topics: list = []

    async def ensure_topic(self, title: str) -> int:
        return 1

    async def send_to_topic(self, thread_id: int, text: str) -> None:
        return None

    async def delete_topic(self, thread_id: int) -> None:
        self.deleted_topics.append(thread_id)

    async def close(self) -> None:
        return None


def _force_env_only(tmp_path):
    S._SETTINGS_FILE = str(tmp_path / "none.yaml")
    S._cache = {}; S._cache_mtime = -1.0; S._last_stat = 0.0; S._loaded_once = False


def _store():
    st = Store(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='supportal_gdpr_'), 't.db')}")
    st.bootstrap()
    return st


def _seed(st, user="u", thread=42):
    t = st.create(user, "U")
    st.attach_thread(t.ticket_id, thread)
    aid = st.add_attachment(user, "in", "a.png", "image/png", b"\x89PNG")
    st.add_message(t.ticket_id, user, "in", "U", "hi", attachment_id=aid)
    st.add_message(t.ticket_id, user, "out", "Operator", "hello", tg_message_id=5)
    return t


# ── store ────────────────────────────────────────────────────────────────────
def test_export_user_returns_all_with_b64_attachment():
    st = _store()
    _seed(st)
    _seed(st, "other", 43)                      # a different user must not leak into the export
    data = st.export_user("u")
    assert len(data["tickets"]) == 1 and len(data["messages"]) == 2 and len(data["attachments"]) == 1
    assert base64.b64decode(data["attachments"][0]["data"]) == b"\x89PNG"
    assert all(m["user_ref"] == "u" for m in data["messages"])


def test_delete_user_erases_and_isolates():
    st = _store()
    _seed(st); _seed(st, "other", 43)
    counts = st.delete_user("u")
    assert counts["messages"] == 2 and counts["attachments"] == 1 and counts["tickets"] == 1
    empty = st.export_user("u")
    assert empty["tickets"] == [] and empty["messages"] == [] and empty["attachments"] == []
    assert len(st.export_user("other")["messages"]) == 2    # the other user is untouched


def test_thread_ids_for_user():
    st = _store()
    _seed(st)
    assert st.thread_ids_for_user("u") == [42]


# ── relay (flag gate + topic deletion) ───────────────────────────────────────
def _relay():
    st = _store()
    tg = _RecTG()
    return Relay(st, tg, "", ""), st, tg


def test_relay_gated_off(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_GDPR", raising=False)
    relay, st, tg = _relay()
    _seed(st)
    assert relay.export_user("u") is None
    assert asyncio.run(relay.delete_user("u")) is None
    assert tg.deleted_topics == [] and len(st.export_user("u")["messages"]) == 2   # nothing touched


def test_relay_delete_removes_topics(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_GDPR", "on")
    relay, st, tg = _relay()
    _seed(st)
    counts = asyncio.run(relay.delete_user("u"))
    assert counts["tickets"] == 1 and tg.deleted_topics == [42]
    assert st.export_user("u")["messages"] == []


# ── endpoints (HMAC + flag) ───────────────────────────────────────────────────
def _client():
    st = _store()
    _seed(st)
    relay = Relay(st, _RecTG(), "", "")
    app = FastAPI(); app.include_router(build_router(relay, _SECRET))
    return TestClient(app)


def _signed(client, path, payload):
    body = json.dumps(payload).encode()
    return client.post(path, content=body,
                       headers={"Content-Type": "application/json", "X-Supportal-Signature": sign(body, _SECRET)})


def test_endpoint_off_404(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.delenv("SUPPORT_GDPR", raising=False)
    client = _client()
    assert _signed(client, "/v1/admin/export", {"user_ref": "u"}).status_code == 404
    assert _signed(client, "/v1/admin/delete", {"user_ref": "u"}).status_code == 404


def test_endpoint_bad_signature_401(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_GDPR", "on")
    client = _client()
    r = client.post("/v1/admin/export", content=b'{"user_ref":"u"}',
                    headers={"Content-Type": "application/json", "X-Supportal-Signature": "bad"})
    assert r.status_code == 401


def test_endpoint_missing_user_ref_400(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_GDPR", "on")
    client = _client()
    assert _signed(client, "/v1/admin/export", {"user_ref": "  "}).status_code == 400


def test_endpoint_export_and_delete_happy(tmp_path, monkeypatch):
    _force_env_only(tmp_path); monkeypatch.setenv("SUPPORT_GDPR", "on")
    client = _client()
    exp = _signed(client, "/v1/admin/export", {"user_ref": "u"})
    assert exp.status_code == 200 and len(exp.json()["messages"]) == 2
    dele = _signed(client, "/v1/admin/delete", {"user_ref": "u"})
    assert dele.status_code == 200 and dele.json()["ok"] and dele.json()["deleted"]["messages"] == 2
    # after erase, the export is empty
    assert _signed(client, "/v1/admin/export", {"user_ref": "u"}).json()["messages"] == []
