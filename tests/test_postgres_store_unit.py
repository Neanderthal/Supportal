"""Reference PostgresStore — the full Store contract against a LIVE Postgres.

Gated: runs only when SUPPORT_TEST_PG_URL points at a reachable Postgres (set it to the throwaway container,
e.g. postgresql://postgres:pg@localhost:55432/supportal), otherwise the whole module is skipped — so CI and
offline runs don't need Postgres. Verifies the same behaviours the SQLite store guarantees (seq cursor, edit/
delete/clear, read receipts, attachments bytes, outbox, GDPR, metrics) on the real backend.
"""
import os

import pytest

from supportal.store import make_store

_PG_URL = os.environ.get("SUPPORT_TEST_PG_URL", "")


def _reachable(url: str) -> bool:
    if not url:
        return False
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(_PG_URL),
                                reason="set SUPPORT_TEST_PG_URL to a reachable Postgres to run the backend test")


@pytest.fixture()
def store():
    from supportal.store_pg import PostgresStore
    st = PostgresStore(_PG_URL)
    st._conn.execute("DROP TABLE IF EXISTS messages, tickets, outbox, attachments, seq_counter CASCADE")
    st._conn.commit()
    st.bootstrap()
    yield st
    st._conn.close()


def test_make_store_dispatch(store):
    from supportal.store import Store
    from supportal.store_pg import PostgresStore
    assert isinstance(make_store(_PG_URL), PostgresStore)
    assert isinstance(make_store("sqlite:///:memory:"), Store)


def test_ticket_lifecycle(store):
    t = store.create("u", "U")
    assert store.get_open_by_user("u").ticket_id == t.ticket_id
    store.attach_thread(t.ticket_id, 55)
    assert store.get_by_thread(55).ticket_id == t.ticket_id
    store.close(t.ticket_id)
    assert store.get_open_by_user("u") is None
    assert store.get_last_by_user("u").status == "closed"
    assert store.thread_ids_for_user("u") == [55]


def test_feed_seq_cursor_and_edit_delete(store):
    t = store.create("u", "U")
    m1 = store.add_message(t.ticket_id, "u", "in", "U", "first")
    m2 = store.add_message(t.ticket_id, "u", "out", "Operator", "reply", tg_message_id=777)
    rows = store.changes_since("u", 0)
    assert [r.id for r in rows] == [m1, m2] and rows[0].seq < rows[1].seq
    cur = rows[-1].seq
    # edit-sync bumps seq → surfaces after the cursor
    store.update_message_by_tg(777, "edited reply")
    ch = store.changes_since("u", cur)
    assert len(ch) == 1 and ch[0].text == "edited reply" and ch[0].deleted is False
    # /del soft-deletes + bumps seq
    store.delete_message_by_tg(777)
    ch2 = store.changes_since("u", ch[0].seq)
    assert len(ch2) == 1 and ch2[0].deleted is True
    assert store.update_message_by_tg(999, "x") is None


def test_clear_and_mark_read(store):
    t = store.create("u", "U")
    store.add_message(t.ticket_id, "u", "in", "U", "a")
    store.add_message(t.ticket_id, "u", "out", "Operator", "b", tg_message_id=5)
    assert store.mark_read("u", 9999) == [5]            # the out message w/ a tg id became read
    assert all(m.read for m in store.changes_since("u", 0) if m.direction == "out")
    n = store.clear_messages("u")
    assert n == 2 and all(m.deleted for m in store.changes_since("u", 0))


def test_attachment_bytes_roundtrip(store):
    t = store.create("u", "U")
    aid = store.add_attachment("u", "in", "a.png", "image/png", b"\x89PNG\x00\x01")
    att = store.get_attachment(aid)
    assert att.data == b"\x89PNG\x00\x01" and att.mime == "image/png" and att.size == 6
    store.add_message(t.ticket_id, "u", "in", "U", "", attachment_id=aid)
    row = store.changes_since("u", 0)[-1]
    assert row.attachment_id == aid and row.attachment_name == "a.png" and row.attachment_mime == "image/png"
    assert store.get_attachment(10_000) is None


def test_outbox(store):
    oid = store.enqueue_outbox("t1", "u", "message", "hi", "op", attempts=1, next_attempt_at=100.0)
    assert oid > 0 and store.outbox_pending_count() == 1
    due = store.outbox_due(now=200.0)
    assert len(due) == 1 and due[0].id == oid
    assert store.outbox_due(now=50.0) == []              # not due yet
    store.outbox_mark_delivered(oid)
    assert store.outbox_pending_count() == 0


def test_changes_since_page_boundary(store):
    # a /clear stamps many rows with ONE seq; a page must not split that seq group (cursor would skip the rest)
    t = store.create("u", "U")
    for i in range(5):
        store.add_message(t.ticket_id, "u", "in", "U", f"m{i}")
    store.clear_messages("u")                            # all 5 → deleted, one shared seq
    rows = store.changes_since("u", 0, limit=3)          # limit < group size
    assert len({r.seq for r in rows if r.deleted}) == 1 and sum(r.deleted for r in rows) == 5


def test_gdpr_and_metrics(store):
    t = store.create("u", "U"); store.attach_thread(t.ticket_id, 7)
    aid = store.add_attachment("u", "in", "a.bin", "application/octet-stream", b"xyz")
    store.add_message(t.ticket_id, "u", "in", "U", "hi", attachment_id=aid)
    store.add_message(t.ticket_id, "u", "out", "Operator", "hello")
    store.create("v", "V")                               # a second user — must not leak
    exp = store.export_user("u")
    assert len(exp["tickets"]) == 1 and len(exp["messages"]) == 2 and len(exp["attachments"]) == 1
    import base64
    assert base64.b64decode(exp["attachments"][0]["data"]) == b"xyz"
    m = store.metrics()
    assert m["tickets_open"] == 2 and m["messages_in"] == 1 and m["messages_out"] == 1 and m["attachments"] == 1
    counts = store.delete_user("u")
    assert counts["messages"] == 2 and counts["attachments"] == 1 and counts["tickets"] == 1
    assert store.export_user("u")["messages"] == [] and len(store.export_user("v")["tickets"]) == 1
