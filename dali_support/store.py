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
