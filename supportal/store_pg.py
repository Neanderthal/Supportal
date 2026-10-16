"""Reference PostgreSQL backend — the same `Store` contract as the default SQLite store, for a
multi-user / fault-tolerant deployment (`SUPPORT_DB_URL=postgresql://…`).

Chosen by `supportal.store.make_store` on the URL scheme. psycopg (v3) is imported LAZILY so the package
stays importable without it (the SQLite default needs nothing extra). Behaviour mirrors `Store` exactly —
the dataclasses (`Ticket`/`OutboxItem`/`Message`/`Attachment`) and method semantics are shared; only the SQL
dialect differs (``%s`` placeholders, ``BIGSERIAL``/``BYTEA``/``DOUBLE PRECISION``, ``RETURNING`` for ids).
`seq` is still bumped atomically via the single-row `seq_counter` (`UPDATE … RETURNING`), so the monotonic
polling cursor is race-free here too — and Postgres genuinely supports multiple concurrent connections.
"""
from __future__ import annotations

import base64
import time
import uuid

from .store import Attachment, Message, OutboxItem, Ticket

_PREFIXES = ("postgresql://", "postgres://")


def is_postgres_url(db_url: str) -> bool:
    return db_url.startswith(_PREFIXES)


def _ticket(row: dict) -> Ticket:
    return Ticket(ticket_id=row["ticket_id"], user_ref=row["user_ref"], display_name=row["display_name"],
                  thread_id=row["thread_id"], status=row["status"],
                  created_at=row["created_at"], last_at=row["last_at"])


def _outbox(row: dict) -> OutboxItem:
    return OutboxItem(id=row["id"], ticket_id=row["ticket_id"], user_ref=row["user_ref"],
                      event=row["event"], text=row["text"], operator=row["operator"],
                      status=row["status"], attempts=row["attempts"],
                      created_at=row["created_at"], next_attempt_at=row["next_attempt_at"])


def _message(row: dict) -> Message:
    return Message(id=row["id"], ticket_id=row["ticket_id"], user_ref=row["user_ref"],
                   direction=row["direction"], author=row["author"], text=row["text"],
                   created_at=row["created_at"], tg_message_id=row["tg_message_id"] or 0,
                   deleted=bool(row["deleted"]), seq=row["seq"] or 0,
                   kind=row.get("kind") or "msg", read=bool(row.get("read")),
                   attachment_id=row.get("attachment_id") or 0,
                   attachment_name=row.get("attachment_name") or "",
                   attachment_mime=row.get("attachment_mime") or "")


class PostgresStore:
    def __init__(self, db_url: str) -> None:
        import psycopg
        from psycopg.rows import dict_row
        self._conn = psycopg.connect(db_url, row_factory=dict_row)

    def _one(self, sql: str, *params):
        return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, *params):
        return self._conn.execute(sql, params).fetchall()

    def bootstrap(self) -> None:
        c = self._conn
        c.execute("""CREATE TABLE IF NOT EXISTS tickets (
                         ticket_id   TEXT PRIMARY KEY,
                         user_ref    TEXT NOT NULL,
                         display_name TEXT NOT NULL DEFAULT '',
                         thread_id   BIGINT,
                         status      TEXT NOT NULL DEFAULT 'open',
                         created_at  DOUBLE PRECISION NOT NULL,
                         last_at     DOUBLE PRECISION NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_tickets_user ON tickets(user_ref)")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tickets_thread ON tickets(thread_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS outbox (
                         id          BIGSERIAL PRIMARY KEY,
                         ticket_id   TEXT NOT NULL,
                         user_ref    TEXT NOT NULL,
                         event       TEXT NOT NULL DEFAULT 'message',
                         text        TEXT NOT NULL DEFAULT '',
                         operator    TEXT NOT NULL DEFAULT '',
                         status      TEXT NOT NULL DEFAULT 'pending',
                         attempts    INTEGER NOT NULL DEFAULT 0,
                         created_at  DOUBLE PRECISION NOT NULL,
                         next_attempt_at DOUBLE PRECISION NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox(status, next_attempt_at)")
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
                         id          BIGSERIAL PRIMARY KEY,
                         ticket_id   TEXT NOT NULL,
                         user_ref    TEXT NOT NULL,
                         direction   TEXT NOT NULL,
                         author      TEXT NOT NULL DEFAULT '',
                         text        TEXT NOT NULL DEFAULT '',
                         created_at  DOUBLE PRECISION NOT NULL,
                         tg_message_id BIGINT,
                         deleted     INTEGER NOT NULL DEFAULT 0,
                         seq         BIGINT,
                         kind        TEXT NOT NULL DEFAULT 'msg',
                         read        INTEGER NOT NULL DEFAULT 0,
                         attachment_id BIGINT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_messages_seq ON messages(user_ref, seq)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_messages_tg ON messages(tg_message_id)")
        c.execute("CREATE TABLE IF NOT EXISTS seq_counter (v BIGINT NOT NULL)")
        if self._one("SELECT count(*) AS n FROM seq_counter")["n"] == 0:
            c.execute("INSERT INTO seq_counter(v) SELECT COALESCE(MAX(seq),0) FROM messages")
        c.execute("""CREATE TABLE IF NOT EXISTS attachments (
                         id          BIGSERIAL PRIMARY KEY,
                         user_ref    TEXT NOT NULL,
                         direction   TEXT NOT NULL DEFAULT 'in',
                         filename    TEXT NOT NULL DEFAULT '',
                         mime        TEXT NOT NULL DEFAULT '',
                         size        BIGINT NOT NULL DEFAULT 0,
                         data        BYTEA,
                         created_at  DOUBLE PRECISION NOT NULL)""")
        c.commit()

    # ── tickets ────────────────────────────────────────────────────────────────
    def get_open_by_user(self, user_ref: str) -> Ticket | None:
        r = self._one("SELECT * FROM tickets WHERE user_ref=%s AND status='open' ORDER BY last_at DESC LIMIT 1",
                      user_ref)
        return _ticket(r) if r else None

    def get_last_by_user(self, user_ref: str) -> Ticket | None:
        r = self._one("SELECT * FROM tickets WHERE user_ref=%s ORDER BY last_at DESC LIMIT 1", user_ref)
        return _ticket(r) if r else None

    def get_by_thread(self, thread_id: int) -> Ticket | None:
        r = self._one("SELECT * FROM tickets WHERE thread_id=%s", thread_id)
        return _ticket(r) if r else None

    def create(self, user_ref: str, display_name: str) -> Ticket:
        now = time.time()
        tid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO tickets(ticket_id,user_ref,display_name,thread_id,status,created_at,last_at)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s)", (tid, user_ref, display_name, None, "open", now, now))
        self._conn.commit()
        return Ticket(tid, user_ref, display_name, None, "open", now, now)

    def attach_thread(self, ticket_id: str, thread_id: int) -> None:
        self._conn.execute("UPDATE tickets SET thread_id=%s, last_at=%s WHERE ticket_id=%s",
                           (thread_id, time.time(), ticket_id))
        self._conn.commit()

    def touch(self, ticket_id: str) -> None:
        self._conn.execute("UPDATE tickets SET last_at=%s WHERE ticket_id=%s", (time.time(), ticket_id))
        self._conn.commit()

    def close(self, ticket_id: str) -> None:
        self._conn.execute("UPDATE tickets SET status='closed', last_at=%s WHERE ticket_id=%s",
                           (time.time(), ticket_id))
        self._conn.commit()

    # ── outbox ─────────────────────────────────────────────────────────────────
    def enqueue_outbox(self, ticket_id: str, user_ref: str, event: str, text: str, operator: str,
                       attempts: int, next_attempt_at: float) -> int:
        r = self._one(
            "INSERT INTO outbox(ticket_id,user_ref,event,text,operator,status,attempts,created_at,next_attempt_at)"
            " VALUES(%s,%s,%s,%s,%s,'pending',%s,%s,%s) RETURNING id",
            ticket_id, user_ref, event, text, operator, attempts, time.time(), next_attempt_at)
        self._conn.commit()
        return r["id"]

    def outbox_due(self, now: float, limit: int = 50) -> list[OutboxItem]:
        rows = self._all(
            "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=%s ORDER BY created_at LIMIT %s",
            now, limit)
        return [_outbox(r) for r in rows]

    def outbox_mark_delivered(self, item_id: int) -> None:
        self._conn.execute("UPDATE outbox SET status='delivered' WHERE id=%s", (item_id,)); self._conn.commit()

    def outbox_mark_dead(self, item_id: int) -> None:
        self._conn.execute("UPDATE outbox SET status='dead' WHERE id=%s", (item_id,)); self._conn.commit()

    def outbox_mark_retry(self, item_id: int, attempts: int, next_attempt_at: float) -> None:
        self._conn.execute("UPDATE outbox SET attempts=%s, next_attempt_at=%s WHERE id=%s",
                           (attempts, next_attempt_at, item_id)); self._conn.commit()

    def outbox_pending_count(self) -> int:
        return self._one("SELECT count(*) AS n FROM outbox WHERE status='pending'")["n"]

    # ── messages ───────────────────────────────────────────────────────────────
    def _next_seq(self) -> int:
        return self._one("UPDATE seq_counter SET v = v + 1 RETURNING v")["v"]

    def add_message(self, ticket_id: str, user_ref: str, direction: str, author: str, text: str,
                    tg_message_id: int = 0, attachment_id: int | None = None, kind: str = "msg") -> int:
        r = self._one(
            "INSERT INTO messages(ticket_id,user_ref,direction,author,text,created_at,tg_message_id,deleted,seq,attachment_id,kind)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s) RETURNING id",
            ticket_id, user_ref, direction, author, text, time.time(), tg_message_id or None,
            self._next_seq(), attachment_id or None, kind)
        self._conn.commit()
        return r["id"]

    def add_attachment(self, user_ref: str, direction: str, filename: str, mime: str, data: bytes) -> int:
        r = self._one(
            "INSERT INTO attachments(user_ref,direction,filename,mime,size,data,created_at)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            user_ref, direction, filename, mime, len(data), data, time.time())
        self._conn.commit()
        return r["id"]

    def get_attachment(self, attachment_id: int) -> Attachment | None:
        r = self._one("SELECT * FROM attachments WHERE id=%s", attachment_id)
        if not r:
            return None
        return Attachment(id=r["id"], user_ref=r["user_ref"], direction=r["direction"],
                          filename=r["filename"], mime=r["mime"], size=r["size"],
                          data=bytes(r["data"]) if r["data"] is not None else b"", created_at=r["created_at"])

    def _by_tg(self, tg_message_id: int) -> Message | None:
        r = self._one("SELECT * FROM messages WHERE tg_message_id=%s AND direction='out' ORDER BY id DESC LIMIT 1",
                      tg_message_id)
        return _message(r) if r else None

    def update_message_by_tg(self, tg_message_id: int, text: str) -> Message | None:
        if not tg_message_id or self._by_tg(tg_message_id) is None:
            return None
        self._conn.execute("UPDATE messages SET text=%s, seq=%s WHERE tg_message_id=%s AND direction='out'",
                           (text, self._next_seq(), tg_message_id))
        self._conn.commit()
        return self._by_tg(tg_message_id)

    def delete_message_by_tg(self, tg_message_id: int) -> Message | None:
        if not tg_message_id or self._by_tg(tg_message_id) is None:
            return None
        self._conn.execute("UPDATE messages SET deleted=1, seq=%s WHERE tg_message_id=%s AND direction='out'",
                           (self._next_seq(), tg_message_id))
        self._conn.commit()
        return self._by_tg(tg_message_id)

    def clear_messages(self, user_ref: str) -> int:
        seq = self._next_seq()
        cur = self._conn.execute("UPDATE messages SET deleted=1, seq=%s WHERE user_ref=%s AND deleted=0",
                                 (seq, user_ref))
        self._conn.commit()
        return cur.rowcount

    def changes_since(self, user_ref: str, after_seq: int, limit: int = 200) -> list[Message]:
        rows = self._all(
            "SELECT m.*, a.filename AS attachment_name, a.mime AS attachment_mime "
            "FROM messages m LEFT JOIN attachments a ON a.id = m.attachment_id "
            "WHERE m.user_ref=%s AND m.seq>%s ORDER BY m.seq, m.id LIMIT %s", user_ref, after_seq, limit)
        if len(rows) == limit:   # never end a page mid-seq (a /clear batch shares one seq) — see Store.changes_since
            rows.extend(self._all(
                "SELECT m.*, a.filename AS attachment_name, a.mime AS attachment_mime "
                "FROM messages m LEFT JOIN attachments a ON a.id = m.attachment_id "
                "WHERE m.user_ref=%s AND m.seq=%s AND m.id>%s ORDER BY m.id",
                user_ref, rows[-1]["seq"], rows[-1]["id"]))
        return [_message(r) for r in rows]

    def mark_read(self, user_ref: str, up_to_seq: int) -> list[int]:
        rows = self._all(
            "SELECT tg_message_id FROM messages WHERE user_ref=%s AND direction='out' AND read=0 "
            "AND seq<=%s AND tg_message_id IS NOT NULL", user_ref, up_to_seq)
        self._conn.execute("UPDATE messages SET read=1 WHERE user_ref=%s AND direction='out' AND read=0 AND seq<=%s",
                           (user_ref, up_to_seq))
        self._conn.commit()
        return [r["tg_message_id"] for r in rows if r["tg_message_id"]]

    # ── GDPR ───────────────────────────────────────────────────────────────────
    def export_user(self, user_ref: str) -> dict:
        tickets = [dict(r) for r in self._all("SELECT * FROM tickets WHERE user_ref=%s ORDER BY created_at", user_ref)]
        messages = [dict(r) for r in self._all("SELECT * FROM messages WHERE user_ref=%s ORDER BY id", user_ref)]
        attachments = []
        for r in self._all("SELECT * FROM attachments WHERE user_ref=%s ORDER BY id", user_ref):
            a = dict(r)
            a["data"] = base64.b64encode(bytes(r["data"])).decode("ascii") if r["data"] is not None else ""
            attachments.append(a)
        return {"user_ref": user_ref, "tickets": tickets, "messages": messages, "attachments": attachments}

    def delete_user(self, user_ref: str) -> dict:
        counts = {}
        for table in ("messages", "attachments", "tickets", "outbox"):
            cur = self._conn.execute(f"DELETE FROM {table} WHERE user_ref=%s", (user_ref,))
            counts[table] = cur.rowcount
        self._conn.commit()
        return counts

    def thread_ids_for_user(self, user_ref: str) -> list[int]:
        return [r["thread_id"] for r in
                self._all("SELECT thread_id FROM tickets WHERE user_ref=%s AND thread_id IS NOT NULL", user_ref)]

    # ── metrics ────────────────────────────────────────────────────────────────
    def metrics(self) -> dict:
        n = lambda sql: self._one(sql)["n"]  # noqa: E731
        return {
            "tickets_open": n("SELECT count(*) AS n FROM tickets WHERE status='open'"),
            "tickets_closed": n("SELECT count(*) AS n FROM tickets WHERE status='closed'"),
            "messages_in": n("SELECT count(*) AS n FROM messages WHERE direction='in' AND deleted=0"),
            "messages_out": n("SELECT count(*) AS n FROM messages WHERE direction='out' AND deleted=0"),
            "attachments": n("SELECT count(*) AS n FROM attachments"),
            "outbox_pending": n("SELECT count(*) AS n FROM outbox WHERE status='pending'"),
        }
