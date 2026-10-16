"""Persistent mapping ticket ↔ forum topic ↔ user. Survives a restart (the topic must not be lost).

Default — local SQLite (zero infrastructure, the project is self-contained). For a multi-user setup
swap in Postgres (same interface/schema) — see docs/integration.md.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass


@dataclass
class Ticket:
    ticket_id: str
    user_ref: str
    display_name: str
    thread_id: int | None
    status: str          # open | closed
    created_at: float
    last_at: float


@dataclass
class OutboxItem:
    """An unconfirmed operator reply (the callback to the product returned delivered=0 or failed) — awaiting retry."""
    id: int
    ticket_id: str
    user_ref: str
    event: str           # message | ticket_closed
    text: str
    operator: str
    status: str          # pending | delivered | dead
    attempts: int        # how many delivery attempts have already been made (the live attempt = 1)
    created_at: float
    next_attempt_at: float


@dataclass
class Message:
    """A conversation row (the ticket feed). The single source of truth for widget polling and recovery."""
    id: int
    ticket_id: str
    user_ref: str
    direction: str       # in (user → operator) | out (operator/system → user)
    author: str          # who wrote it (for display): the user's name (in) or "Operator" (out)
    text: str
    created_at: float
    tg_message_id: int = 0   # message_id in Telegram (for edit-sync and /del; 0 — not from a topic)
    deleted: bool = False    # soft delete (the operator recalled it with /del) — the widget removes the bubble
    seq: int = 0             # monotonic cursor of CHANGES (insert/edit/delete); polling follows it
    kind: str = "msg"        # msg (normal) | csat_request (close-notice that asks for a rating) | csat (the rating)
    attachment_id: int = 0   # → attachments.id when this row carries a file (0 — text only)
    attachment_name: str = ""  # filename (populated via JOIN in changes_since; "" otherwise)
    attachment_mime: str = ""  # MIME type (populated via JOIN in changes_since)


@dataclass
class Attachment:
    """A stored file (bytes in the DB). Feature SUPPORT_ATTACHMENTS; served via /v1/widget/attachment/{id}."""
    id: int
    user_ref: str
    direction: str       # in | out
    filename: str
    mime: str
    size: int
    data: bytes
    created_at: float


class Store:
    def __init__(self, db_url: str) -> None:
        if not db_url.startswith("sqlite:///"):
            raise NotImplementedError(
                "The skeleton ships only a SQLite backend; for Postgres implement the same interface (docs/integration.md).")
        self._path = db_url[len("sqlite:///"):]
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def bootstrap(self) -> None:
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS tickets (
                   ticket_id   TEXT PRIMARY KEY,
                   user_ref    TEXT NOT NULL,
                   display_name TEXT NOT NULL DEFAULT '',
                   thread_id   INTEGER,
                   status      TEXT NOT NULL DEFAULT 'open',
                   created_at  REAL NOT NULL,
                   last_at     REAL NOT NULL)""")
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_tickets_user ON tickets(user_ref)")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tickets_thread ON tickets(thread_id)")
        # outbox — operator replies the product did not confirm as delivered (delivered=0 / callback failed).
        # A pure HTTP contract: the relay retries the callback knowing NOTHING about the product's sessions (boundary A).
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS outbox (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   ticket_id   TEXT NOT NULL,
                   user_ref    TEXT NOT NULL,
                   event       TEXT NOT NULL DEFAULT 'message',
                   text        TEXT NOT NULL DEFAULT '',
                   operator    TEXT NOT NULL DEFAULT '',
                   status      TEXT NOT NULL DEFAULT 'pending',
                   attempts    INTEGER NOT NULL DEFAULT 0,
                   created_at  REAL NOT NULL,
                   next_attempt_at REAL NOT NULL)""")
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox(status, next_attempt_at)")
        # messages — the conversation feed (both sides). The source of truth for browser-widget polling
        # (GET /v1/widget/messages?since=<seq>) and recovery on reload. The monotonic `seq`
        # (grows on insert/edit/delete) = the polling cursor (the widget sends the last seq it saw → edits and
        # deletions of old rows also reach it). Persistent → survives a restart and an offline user
        # (the operator reply is not lost: it is picked up on the next poll).
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   ticket_id   TEXT NOT NULL,
                   user_ref    TEXT NOT NULL,
                   direction   TEXT NOT NULL,
                   author      TEXT NOT NULL DEFAULT '',
                   text        TEXT NOT NULL DEFAULT '',
                   created_at  REAL NOT NULL,
                   tg_message_id INTEGER,
                   deleted     INTEGER NOT NULL DEFAULT 0,
                   seq         INTEGER,
                   kind        TEXT NOT NULL DEFAULT 'msg')""")
        # Migrate an existing table (the first widget deploy created messages without the new columns): add
        # the missing ones + backfill seq=id for old rows (id is monotonic → a valid starting cursor).
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        if "tg_message_id" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN tg_message_id INTEGER")
        if "deleted" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        if "seq" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN seq INTEGER")
            self._conn.execute("UPDATE messages SET seq=id WHERE seq IS NULL")
        if "attachment_id" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN attachment_id INTEGER")
        if "kind" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'msg'")
        # polling follows seq (insert+edit+delete) + we look up the row by tg_message_id on edit/del
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_seq ON messages(user_ref, seq)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_tg ON messages(tg_message_id)")
        # attachments — file bytes (feature SUPPORT_ATTACHMENTS). Kept in the DB (one volume = the DB),
        # bounded by a size cap at the upload boundary. Referenced from messages.attachment_id.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS attachments (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_ref    TEXT NOT NULL,
                   direction   TEXT NOT NULL DEFAULT 'in',
                   filename    TEXT NOT NULL DEFAULT '',
                   mime        TEXT NOT NULL DEFAULT '',
                   size        INTEGER NOT NULL DEFAULT 0,
                   data        BLOB,
                   created_at  REAL NOT NULL)""")
        self._conn.commit()

    def _row(self, row: sqlite3.Row) -> Ticket:
        return Ticket(ticket_id=row["ticket_id"], user_ref=row["user_ref"], display_name=row["display_name"],
                      thread_id=row["thread_id"], status=row["status"],
                      created_at=row["created_at"], last_at=row["last_at"])

    def get_open_by_user(self, user_ref: str) -> Ticket | None:
        cur = self._conn.execute(
            "SELECT * FROM tickets WHERE user_ref=? AND status='open' ORDER BY last_at DESC LIMIT 1", (user_ref,))
        row = cur.fetchone()
        return self._row(row) if row else None

    def get_last_by_user(self, user_ref: str) -> Ticket | None:
        """The user's most recent ticket regardless of status (open OR closed). Used to attach a post-close
        CSAT rating to the ticket/topic it refers to (the just-closed one)."""
        cur = self._conn.execute(
            "SELECT * FROM tickets WHERE user_ref=? ORDER BY last_at DESC LIMIT 1", (user_ref,))
        row = cur.fetchone()
        return self._row(row) if row else None

    def get_by_thread(self, thread_id: int) -> Ticket | None:
        cur = self._conn.execute("SELECT * FROM tickets WHERE thread_id=?", (thread_id,))
        row = cur.fetchone()
        return self._row(row) if row else None

    def create(self, user_ref: str, display_name: str) -> Ticket:
        now = time.time()
        tid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO tickets(ticket_id,user_ref,display_name,thread_id,status,created_at,last_at)"
            " VALUES(?,?,?,?,?,?,?)", (tid, user_ref, display_name, None, "open", now, now))
        self._conn.commit()
        return Ticket(tid, user_ref, display_name, None, "open", now, now)

    def attach_thread(self, ticket_id: str, thread_id: int) -> None:
        self._conn.execute("UPDATE tickets SET thread_id=?, last_at=? WHERE ticket_id=?",
                           (thread_id, time.time(), ticket_id))
        self._conn.commit()

    def touch(self, ticket_id: str) -> None:
        self._conn.execute("UPDATE tickets SET last_at=? WHERE ticket_id=?", (time.time(), ticket_id))
        self._conn.commit()

    def close(self, ticket_id: str) -> None:
        self._conn.execute("UPDATE tickets SET status='closed', last_at=? WHERE ticket_id=?",
                           (time.time(), ticket_id))
        self._conn.commit()

    # ── outbox (retrying operator-reply delivery) ──────────────────────────────
    def _outbox_row(self, row: sqlite3.Row) -> OutboxItem:
        return OutboxItem(id=row["id"], ticket_id=row["ticket_id"], user_ref=row["user_ref"],
                          event=row["event"], text=row["text"], operator=row["operator"],
                          status=row["status"], attempts=row["attempts"],
                          created_at=row["created_at"], next_attempt_at=row["next_attempt_at"])

    def enqueue_outbox(self, ticket_id: str, user_ref: str, event: str, text: str, operator: str,
                       attempts: int, next_attempt_at: float) -> int:
        cur = self._conn.execute(
            "INSERT INTO outbox(ticket_id,user_ref,event,text,operator,status,attempts,created_at,next_attempt_at)"
            " VALUES(?,?,?,?,?,'pending',?,?,?)",
            (ticket_id, user_ref, event, text, operator, attempts, time.time(), next_attempt_at))
        self._conn.commit()
        return cur.lastrowid or 0

    def outbox_due(self, now: float, limit: int = 50) -> list[OutboxItem]:
        cur = self._conn.execute(
            "SELECT * FROM outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY created_at LIMIT ?",
            (now, limit))
        return [self._outbox_row(r) for r in cur.fetchall()]

    def outbox_mark_delivered(self, item_id: int) -> None:
        self._conn.execute("UPDATE outbox SET status='delivered' WHERE id=?", (item_id,))
        self._conn.commit()

    def outbox_mark_dead(self, item_id: int) -> None:
        self._conn.execute("UPDATE outbox SET status='dead' WHERE id=?", (item_id,))
        self._conn.commit()

    def outbox_mark_retry(self, item_id: int, attempts: int, next_attempt_at: float) -> None:
        self._conn.execute("UPDATE outbox SET attempts=?, next_attempt_at=? WHERE id=?",
                           (attempts, next_attempt_at, item_id))
        self._conn.commit()

    def outbox_pending_count(self) -> int:
        return self._conn.execute("SELECT count(*) FROM outbox WHERE status='pending'").fetchone()[0]

    # ── messages (conversation feed for widget polling) ────────────────────────
    def _message_row(self, row: sqlite3.Row) -> Message:
        keys = row.keys()
        return Message(id=row["id"], ticket_id=row["ticket_id"], user_ref=row["user_ref"],
                       direction=row["direction"], author=row["author"], text=row["text"],
                       created_at=row["created_at"], tg_message_id=row["tg_message_id"] or 0,
                       deleted=bool(row["deleted"]), seq=row["seq"] or 0,
                       kind=(row["kind"] or "msg") if "kind" in keys else "msg",
                       attachment_id=(row["attachment_id"] or 0) if "attachment_id" in keys else 0,
                       attachment_name=(row["attachment_name"] or "") if "attachment_name" in keys else "",
                       attachment_mime=(row["attachment_mime"] or "") if "attachment_mime" in keys else "")

    def _next_seq(self) -> int:
        """Monotonic change cursor: max(seq)+1. A single writer (SQLite serializes) → no races."""
        return (self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages").fetchone()[0])

    def add_message(self, ticket_id: str, user_ref: str, direction: str, author: str, text: str,
                    tg_message_id: int = 0, attachment_id: int | None = None, kind: str = "msg") -> int:
        """Append a row to the ticket feed. tg_message_id — the Telegram message id (for edit/del);
        attachment_id — → attachments.id when the row carries a file; kind — row type (msg|csat_request|csat).
        Returns the id."""
        cur = self._conn.execute(
            "INSERT INTO messages(ticket_id,user_ref,direction,author,text,created_at,tg_message_id,deleted,seq,attachment_id,kind)"
            " VALUES(?,?,?,?,?,?,?,0,?,?,?)",
            (ticket_id, user_ref, direction, author, text, time.time(), tg_message_id or None,
             self._next_seq(), attachment_id or None, kind))
        self._conn.commit()
        return cur.lastrowid or 0

    # ── attachments (file bytes in the DB; feature SUPPORT_ATTACHMENTS) ─────────
    def add_attachment(self, user_ref: str, direction: str, filename: str, mime: str, data: bytes) -> int:
        """Store file bytes; returns the attachment id (referenced by a message's attachment_id)."""
        cur = self._conn.execute(
            "INSERT INTO attachments(user_ref,direction,filename,mime,size,data,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_ref, direction, filename, mime, len(data), sqlite3.Binary(data), time.time()))
        self._conn.commit()
        return cur.lastrowid or 0

    def get_attachment(self, attachment_id: int) -> Attachment | None:
        cur = self._conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,))
        row = cur.fetchone()
        if not row:
            return None
        return Attachment(id=row["id"], user_ref=row["user_ref"], direction=row["direction"],
                          filename=row["filename"], mime=row["mime"], size=row["size"],
                          data=bytes(row["data"]) if row["data"] is not None else b"",
                          created_at=row["created_at"])

    def _by_tg(self, tg_message_id: int) -> Message | None:
        cur = self._conn.execute(
            "SELECT * FROM messages WHERE tg_message_id=? AND direction='out' ORDER BY id DESC LIMIT 1",
            (tg_message_id,))
        row = cur.fetchone()
        return self._message_row(row) if row else None

    def update_message_by_tg(self, tg_message_id: int, text: str) -> Message | None:
        """Edit-sync: the operator edited a message in Telegram → update the text + bump seq. None — not found."""
        if not tg_message_id or self._by_tg(tg_message_id) is None:
            return None
        self._conn.execute("UPDATE messages SET text=?, seq=? WHERE tg_message_id=? AND direction='out'",
                           (text, self._next_seq(), tg_message_id))
        self._conn.commit()
        return self._by_tg(tg_message_id)

    def delete_message_by_tg(self, tg_message_id: int) -> Message | None:
        """/del: mark the message deleted + bump seq → the widget removes the bubble. None — not found."""
        if not tg_message_id or self._by_tg(tg_message_id) is None:
            return None
        self._conn.execute("UPDATE messages SET deleted=1, seq=? WHERE tg_message_id=? AND direction='out'",
                           (self._next_seq(), tg_message_id))
        self._conn.commit()
        return self._by_tg(tg_message_id)

    def clear_messages(self, user_ref: str) -> int:
        """/clear: mark the user's ENTIRE feed deleted (one seq for the batch) → the widget removes all bubbles.
        Returns the number of affected rows. Soft (deleted=1) so seq stays monotonic and reaches the client via polling."""
        seq = self._next_seq()
        cur = self._conn.execute(
            "UPDATE messages SET deleted=1, seq=? WHERE user_ref=? AND deleted=0", (seq, user_ref))
        self._conn.commit()
        return cur.rowcount

    def changes_since(self, user_ref: str, after_seq: int, limit: int = 200) -> list[Message]:
        """The user's rows with seq > after_seq (insert/edit/delete), ascending by seq. after_seq=0 → the whole feed.

        Includes deleted ones (deleted=1) — the widget removes their bubbles; and edited ones — it updates the text.
        The client cursor = max(seq) of the response."""
        cur = self._conn.execute(
            "SELECT m.*, a.filename AS attachment_name, a.mime AS attachment_mime "
            "FROM messages m LEFT JOIN attachments a ON a.id = m.attachment_id "
            "WHERE m.user_ref=? AND m.seq>? ORDER BY m.seq LIMIT ?", (user_ref, after_seq, limit))
        return [self._message_row(r) for r in cur.fetchall()]
