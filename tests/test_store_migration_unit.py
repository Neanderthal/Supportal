"""Store.bootstrap() — legacy schema migration + non-SQLite guard. Offline, no network.

The first widget deploy created `messages` without tg_message_id/deleted/seq. bootstrap() must
ALTER-add the missing columns and backfill seq=id for old rows, keeping the table fully usable.
"""
import sqlite3

import pytest

from supportal.store import Store


def _legacy_messages_db(path) -> None:
    """Create a pre-widget `messages` table (no tg_message_id/deleted/seq) with one old row."""
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE messages (
                       id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, user_ref TEXT,
                       direction TEXT, author TEXT, text TEXT, created_at REAL)""")
    con.execute("INSERT INTO messages(ticket_id,user_ref,direction,author,text,created_at)"
                " VALUES('t1','eng@x','in','Ivanov','old row',1.0)")
    con.commit()
    con.close()


def test_bootstrap_migrates_legacy_messages_table(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_messages_db(path)

    store = Store(f"sqlite:///{path}")
    store.bootstrap()                                   # must ALTER-add missing columns + backfill seq=id

    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(messages)")}
    assert {"tg_message_id", "deleted", "seq"} <= cols   # new columns present after migration

    # old row is readable via the normal API, seq backfilled (= id), not deleted
    rows = store.changes_since("eng@x", 0)
    assert len(rows) == 1 and rows[0].text == "old row"
    assert rows[0].seq == rows[0].id and rows[0].deleted is False

    # the migrated table stays functional: a new append bumps seq above the backfilled one
    mid = store.add_message("t1", "eng@x", "out", "Operator", "new row")
    tail = store.changes_since("eng@x", rows[0].seq)
    assert [r.id for r in tail] == [mid] and tail[0].seq > rows[0].seq


def test_bootstrap_is_idempotent_on_migrated_db(tmp_path):
    path = tmp_path / "legacy.db"
    _legacy_messages_db(path)
    Store(f"sqlite:///{path}").bootstrap()
    # a second bootstrap (e.g. a restart) must not fail or duplicate columns
    store2 = Store(f"sqlite:///{path}")
    store2.bootstrap()
    assert len(store2.changes_since("eng@x", 0)) == 1


def test_store_rejects_non_sqlite_url():
    with pytest.raises(NotImplementedError):
        Store("postgresql://localhost/db")


# ── atomic seq counter + WAL (arch review #1/#2) ───────────────────────────────
def test_bootstrap_enables_wal_and_seeds_seq_counter(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'fresh.db'}")
    store.bootstrap()
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store._conn.execute("SELECT v FROM seq_counter").fetchone()[0] == 0   # empty feed → 0


def test_seq_is_atomic_monotonic_and_unique(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'fresh.db'}")
    store.bootstrap()
    t = store.create("u1", "U1")
    for i in range(5):
        store.add_message(t.ticket_id, "u1", "in", "U1", f"m{i}")
    seqs = [r.seq for r in store.changes_since("u1", 0)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)                  # strictly increasing, unique
    assert store._conn.execute("SELECT v FROM seq_counter").fetchone()[0] == max(seqs)
    # /clear bumps the counter once and stamps the whole batch with that single seq
    before = max(seqs)
    store.clear_messages("u1")
    assert store._conn.execute("SELECT v FROM seq_counter").fetchone()[0] == before + 1
    cleared = store.changes_since("u1", before)
    assert cleared and len({r.seq for r in cleared}) == 1 and all(r.deleted for r in cleared)


def test_seq_counter_seeded_from_existing_max_on_upgrade(tmp_path):
    # a DB that already has messages+seq but no seq_counter (pre-upgrade) → counter seeds to max, not 0
    path = tmp_path / "pre.db"
    con = sqlite3.connect(str(path))
    con.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, user_ref TEXT,
                       direction TEXT, author TEXT DEFAULT '', text TEXT DEFAULT '', created_at REAL,
                       tg_message_id INTEGER, deleted INTEGER DEFAULT 0, seq INTEGER, kind TEXT DEFAULT 'msg')""")
    con.execute("INSERT INTO messages(ticket_id,user_ref,direction,created_at,seq) VALUES('t','u','in',1.0,7)")
    con.commit(); con.close()
    store = Store(f"sqlite:///{path}"); store.bootstrap()
    assert store._conn.execute("SELECT v FROM seq_counter").fetchone()[0] == 7   # seeded from max(seq)
    store.add_message("t", "u", "out", "Operator", "next")
    assert store.changes_since("u", 7)[0].seq == 8                               # continues above the max
