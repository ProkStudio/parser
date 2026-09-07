"""SQLite persistence. A checkpoint commits results and cursor atomically."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .domain import AppError, bounded_int, now


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise AppError("База создана более новой версией Parser. Обновите приложение.", 500)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY, title TEXT NOT NULL, username TEXT NOT NULL,
                    last_id INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, source_id INTEGER,
                    options TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued'
                        CHECK(state IN ('queued','running','waiting','paused','cancelled','failed','completed')),
                    cursor INTEGER NOT NULL DEFAULT 0, upper_id INTEGER,
                    scanned INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0,
                    stop_request TEXT, error TEXT, wait_until TEXT, completion TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_source ON jobs(source_key)
                    WHERE state IN ('queued','running','waiting');
                CREATE INDEX IF NOT EXISTS job_queue ON jobs(state, created_at);
                CREATE TABLE IF NOT EXISTS messages (
                    source_id INTEGER NOT NULL REFERENCES sources(id), message_id INTEGER NOT NULL,
                    sent_at TEXT NOT NULL, text TEXT NOT NULL, media TEXT NOT NULL,
                    views INTEGER NOT NULL, link TEXT NOT NULL,
                    PRIMARY KEY (source_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS message_dates ON messages(sent_at DESC, source_id, message_id);
                CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value REAL NOT NULL);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.create_function("CASEFOLD", 1, lambda value: value.casefold() if value else "", deterministic=True)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='paused', stop_request=NULL, wait_until=NULL, error=?, updated_at=? WHERE state IN ('running','waiting')", ("Приложение было закрыто. Продолжите сбор с сохранённой позиции.", now()))

    @staticmethod
    def _job(row):
        if row is None:
            raise AppError("Задача не найдена.", 404, "not_found")
        result = dict(row)
        result["options"] = json.loads(result["options"])
        return result

    def job(self, job_id):
        with self.connect() as db:
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def jobs(self, offset=0):
        with self.connect() as db:
            return [self._job(row) for row in db.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT 50 OFFSET ?", (offset,))]

    def enqueue(self, sources, options):
        ids, stamp = [], now()
        try:
            with self.connect() as db:
                for source in sources:
                    job_id = uuid.uuid4().hex
                    db.execute("INSERT INTO jobs(id,source_key,options,created_at,updated_at) VALUES(?,?,?,?,?)", (job_id, source, json.dumps(options, ensure_ascii=False), stamp, stamp))
                    ids.append(job_id)
        except sqlite3.IntegrityError:
            raise AppError("Для одного из источников уже есть активная задача. Дождитесь её завершения или отмените её.", 409, "conflict") from None
        return ids

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running', error=NULL, updated_at=? WHERE id=?", (now(), row["id"]))
            return self._job(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def prepare(self, job_id, source, upper_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO sources(id,title,username,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,username=excluded.username,updated_at=excluded.updated_at", (source["id"], source["title"], source["username"], now()))
            job = self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if job["source_id"] is not None and job["source_id"] != source["id"]:
                raise AppError("Источник изменился. Создайте новую задачу вместо продолжения старой.", 409)
            if job["upper_id"] is None:
                cursor = db.execute("SELECT last_id FROM sources WHERE id=?", (source["id"],)).fetchone()[0] if job["options"]["incremental"] else 0
                db.execute("UPDATE jobs SET source_id=?,cursor=?,upper_id=?,updated_at=? WHERE id=?", (source["id"], cursor, upper_id, now(), job_id))
        return self.job(job_id)

    def checkpoint(self, job_id, source_id, cursor, scanned, messages):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany("""INSERT INTO messages(source_id,message_id,sent_at,text,media,views,link) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source_id,message_id) DO UPDATE SET sent_at=excluded.sent_at,text=excluded.text,
                media=excluded.media,views=excluded.views,link=excluded.link""", [(source_id, m["message_id"], m["sent_at"], m["text"], m["media"], m["views"], m["link"]) for m in messages])
            db.execute("UPDATE jobs SET cursor=MAX(cursor,?),scanned=scanned+?,matched=matched+?,updated_at=? WHERE id=?", (cursor, scanned, len(messages), now(), job_id))
            db.execute("UPDATE sources SET last_id=MAX(last_id,?),updated_at=? WHERE id=?", (cursor, now(), source_id))

    def state(self, job_id, state, *, error=None, wait_until=None, completion=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,error=?,wait_until=?,completion=?,updated_at=? WHERE id=?", (state, error, wait_until, completion, now(), job_id))

    def control(self, job_id, action):
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                job = self._job(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
                state, stop = job["state"], None
                if action == "resume" and state in ("paused", "failed"):
                    state = "queued"
                elif action in ("pause", "cancel") and state in ("running", "waiting"):
                    stop = action
                elif action == "pause" and state == "queued":
                    state = "paused"
                elif action == "cancel" and state in ("queued", "paused", "failed"):
                    state = "cancelled"
                else:
                    raise AppError("Это действие недоступно для текущего состояния задачи.", 409, "conflict")
                db.execute("UPDATE jobs SET state=?,stop_request=?,error=NULL,wait_until=CASE WHEN ? IS NULL THEN NULL ELSE wait_until END,updated_at=? WHERE id=?", (state, stop, stop, now(), job_id))
        except sqlite3.IntegrityError:
            raise AppError("У этого источника уже есть активная задача.", 409, "conflict") from None
        return self.job(job_id)

    def busy(self):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','waiting') LIMIT 1").fetchone())

    def set_cooldown(self, seconds):
        with self.connect() as db:
            db.execute("INSERT INTO runtime(key,value) VALUES('cooldown',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (time.time() + max(1, seconds),))

    def cooldown(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM runtime WHERE key='cooldown'").fetchone()
            return row[0] if row else 0

    def stats(self):
        with self.connect() as db:
            return {"messages": db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], "active": db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running','waiting')").fetchone()[0]}

    def sources(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT s.*,COUNT(m.message_id) AS message_count FROM sources s LEFT JOIN messages m ON m.source_id=s.id GROUP BY s.id ORDER BY s.title")]

    @staticmethod
    def _where(query="", source=""):
        if not isinstance(query, str) or len(query) > 200:
            raise AppError("Поисковый запрос: до 200 символов.")
        clauses, params = [], []
        if query:
            clauses.append("CASEFOLD(m.text) LIKE ? ESCAPE '\\'")
            params.append("%" + query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if source:
            try:
                source = int(source)
                if not -(2**63) < source < 2**63:
                    raise ValueError
            except (ValueError, TypeError):
                raise AppError("Некорректный ID источника.") from None
            clauses.append("m.source_id=?")
            params.append(source)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def messages(self, query="", source="", page=1, page_size=30):
        page = bounded_int(page, "Страница", 1, 1000000)
        page_size = bounded_int(page_size, "Размер страницы", 1, 100)
        where, params = self._where(query, source)
        with self.connect() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*) FROM messages m" + where, params).fetchone()[0]
            rows = db.execute("SELECT m.*,s.title AS source_title FROM messages m JOIN sources s ON s.id=m.source_id" + where + " ORDER BY m.sent_at DESC,m.source_id,m.message_id DESC LIMIT ? OFFSET ?", params + [page_size, (page-1)*page_size])
            return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}

    def iter_messages(self, query="", source=""):
        where, params = self._where(query, source)
        with self.connect() as db:
            db.execute("BEGIN")  # Stable snapshot throughout a streamed export.
            rows = db.execute("SELECT m.*,s.title AS source_title FROM messages m JOIN sources s ON s.id=m.source_id" + where + " ORDER BY m.sent_at DESC,m.source_id,m.message_id DESC", params)
            for row in rows:
                yield dict(row)

    def clear(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running','waiting') LIMIT 1").fetchone():
                raise AppError("Сначала остановите или отмените активные задачи.", 409)
            for table in ("messages", "jobs", "sources"):
                db.execute("DELETE FROM " + table)
