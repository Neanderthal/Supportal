"""Персистентный маппинг тикет ↔ forum-топик ↔ пользователь. Переживает рестарт (топик нельзя терять).

Дефолт — локальный SQLite (ноль инфраструктуры, проект самодостаточен). Для многопользовательского
контура подставить Postgres (тот же интерфейс/схема) — см. docs/integration.md.
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
    """Неподтверждённый ответ оператора (callback продукту вернул delivered=0 или упал) — ждёт повтора."""
    id: int
    ticket_id: str
    user_ref: str
    event: str           # message | ticket_closed
    text: str
    operator: str
    status: str          # pending | delivered | dead
    attempts: int        # сколько попыток доставки уже сделано (живая попытка = 1)
    created_at: float
    next_attempt_at: float


class Store:
    def __init__(self, db_url: str) -> None:
        if not db_url.startswith("sqlite:///"):
            raise NotImplementedError(
                "Скелет несёт только SQLite-бэкенд; для Postgres реализовать тот же интерфейс (docs/integration.md).")
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
        # outbox — ответы оператора, которые продукт не подтвердил доставленными (delivered=0 / callback упал).
        # Чистый HTTP-контракт: relay повторяет callback, не зная НИЧЕГО про сессии продукта (граница A).
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

    # ── outbox (повторная доставка ответа оператора) ───────────────────────────
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
