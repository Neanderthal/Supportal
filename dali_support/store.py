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


@dataclass
class Message:
    """Строка переписки (лента тикета). Единый источник правды для polling виджета и восстановления."""
    id: int
    ticket_id: str
    user_ref: str
    direction: str       # in (пользователь → оператор) | out (оператор/система → пользователь)
    author: str          # кто написал (для показа): имя пользователя / оператора / «Система»
    text: str
    created_at: float
    tg_message_id: int = 0   # message_id в Telegram (для edit-sync и /del; 0 — не из топика)
    deleted: bool = False    # мягкое удаление (оператор отозвал /del) — виджет убирает пузырь
    seq: int = 0             # монотонный курсор ИЗМЕНЕНИЙ (insert/edit/delete); polling идёт по нему


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
        # messages — лента переписки (обе стороны). Источник правды для polling браузерного виджета
        # (GET /v1/widget/messages?after=<id>) и восстановления при перезагрузке. Монотонный AUTOINCREMENT
        # id = курсор polling (виджет шлёт последний виденный id). Персистентна → переживает рестарт и
        # оффлайн-пользователя (ответ оператора не теряется: подтянется при следующем опросе).
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
                   seq         INTEGER)""")
        # Миграция существующей таблицы (первый деплой виджета создал messages без новых колонок): добавить
        # недостающие + бэкофилл seq=id для старых строк (id монотонен → валидный стартовый курсор).
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        if "tg_message_id" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN tg_message_id INTEGER")
        if "deleted" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        if "seq" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN seq INTEGER")
            self._conn.execute("UPDATE messages SET seq=id WHERE seq IS NULL")
        # polling идёт по seq (insert+edit+delete) + по tg_message_id ищем строку при edit/del
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_seq ON messages(user_ref, seq)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS ix_messages_tg ON messages(tg_message_id)")
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

    # ── messages (лента переписки для polling виджета) ─────────────────────────
    def _message_row(self, row: sqlite3.Row) -> Message:
        return Message(id=row["id"], ticket_id=row["ticket_id"], user_ref=row["user_ref"],
                       direction=row["direction"], author=row["author"], text=row["text"],
                       created_at=row["created_at"], tg_message_id=row["tg_message_id"] or 0,
                       deleted=bool(row["deleted"]), seq=row["seq"] or 0)

    def _next_seq(self) -> int:
        """Монотонный курсор изменений: max(seq)+1. Один писатель (SQLite сериализует) → без гонок."""
        return (self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM messages").fetchone()[0])

    def add_message(self, ticket_id: str, user_ref: str, direction: str, author: str, text: str,
                    tg_message_id: int = 0) -> int:
        """Дописать строку в ленту тикета. tg_message_id — id сообщения в Telegram (для edit/del). Возвращает id."""
        cur = self._conn.execute(
            "INSERT INTO messages(ticket_id,user_ref,direction,author,text,created_at,tg_message_id,deleted,seq)"
            " VALUES(?,?,?,?,?,?,?,0,?)",
            (ticket_id, user_ref, direction, author, text, time.time(), tg_message_id or None, self._next_seq()))
        self._conn.commit()
        return cur.lastrowid or 0

    def _by_tg(self, tg_message_id: int) -> Message | None:
        cur = self._conn.execute(
            "SELECT * FROM messages WHERE tg_message_id=? AND direction='out' ORDER BY id DESC LIMIT 1",
            (tg_message_id,))
        row = cur.fetchone()
        return self._message_row(row) if row else None

    def update_message_by_tg(self, tg_message_id: int, text: str) -> Message | None:
        """Edit-sync: оператор отредактировал сообщение в Telegram → обновить текст + поднять seq. None — не нашли."""
        if not tg_message_id or self._by_tg(tg_message_id) is None:
            return None
        self._conn.execute("UPDATE messages SET text=?, seq=? WHERE tg_message_id=? AND direction='out'",
                           (text, self._next_seq(), tg_message_id))
        self._conn.commit()
        return self._by_tg(tg_message_id)

    def delete_message_by_tg(self, tg_message_id: int) -> Message | None:
        """/del: пометить сообщение удалённым + поднять seq → виджет уберёт пузырь. None — не нашли."""
        if not tg_message_id or self._by_tg(tg_message_id) is None:
            return None
        self._conn.execute("UPDATE messages SET deleted=1, seq=? WHERE tg_message_id=? AND direction='out'",
                           (self._next_seq(), tg_message_id))
        self._conn.commit()
        return self._by_tg(tg_message_id)

    def changes_since(self, user_ref: str, after_seq: int, limit: int = 200) -> list[Message]:
        """Строки пользователя с seq > after_seq (insert/edit/delete), по возрастанию seq. after_seq=0 → вся лента.

        Содержит и удалённые (deleted=1) — виджет уберёт их пузыри; и отредактированные — обновит текст.
        Курсор клиента = max(seq) ответа."""
        cur = self._conn.execute(
            "SELECT * FROM messages WHERE user_ref=? AND seq>? ORDER BY seq LIMIT ?", (user_ref, after_seq, limit))
        return [self._message_row(r) for r in cur.fetchall()]
