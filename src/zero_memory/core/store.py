import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import portalocker

from zero_memory.core.errors import ServiceError


def now() -> str:
    return datetime.now(UTC).isoformat()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def atomic_json(path: Path, value) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dumps(value), encoding="utf-8")
    tmp.replace(path)


class Store:
    """SQLite is the fallback's single fact source; exports and FTS are rebuildable."""

    def __init__(self, home: Path):
        self.home = home
        for name in (
            "identity/versions",
            "sources",
            "conversation_store",
            "memory",
            "review_queue",
            "backups",
            "logs",
        ):
            (home / name).mkdir(parents=True, exist_ok=True)
        self.path = home / "memory" / "zero.sqlite3"
        if self.path.exists():
            with sqlite3.connect(self.path, timeout=30) as existing:
                if existing.execute("PRAGMA user_version").fetchone()[0] not in {0, 2}:
                    raise ServiceError("schema_version", "未知数据库版本，拒绝初始化或迁移", 503)
        self._mutex = threading.RLock()
        self._local = threading.local()
        with self.write() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT,
              project_id TEXT, status TEXT, content TEXT, body TEXT NOT NULL);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(id UNINDEXED, content, tokenize='trigram');
            CREATE TABLE IF NOT EXISTS revisions(memory_id TEXT, version INTEGER, body TEXT,
              PRIMARY KEY(memory_id, version));
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, time TEXT, action TEXT,
              actor TEXT, target TEXT, details TEXT);
            CREATE TABLE IF NOT EXISTS messages(key TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT,
              project_id TEXT, conversation_id TEXT, message_id TEXT, content_hash TEXT, role TEXT,
              timestamp TEXT, content TEXT, body TEXT,
              UNIQUE(user_id, agent_id, project_id, conversation_id, message_id, content_hash));
            CREATE INDEX IF NOT EXISTS messages_scope ON messages(user_id,agent_id,project_id,conversation_id);
            CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, body TEXT);
            CREATE TABLE IF NOT EXISTS extraction_tasks(message_key TEXT PRIMARY KEY, status TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS clients(hash TEXT PRIMARY KEY, body TEXT);
            CREATE TABLE IF NOT EXISTS chat_messages(id TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT,
              project_id TEXT, session_id TEXT, role TEXT, content TEXT, timestamp TEXT);
            """)

    @contextmanager
    def lock(self):
        with self._mutex:
            depth = getattr(self._local, "depth", 0)
            if depth:
                self._local.depth += 1
                try:
                    yield
                finally:
                    self._local.depth -= 1
            else:
                with portalocker.Lock(str(self.home / ".write.lock"), timeout=30):
                    self._local.depth = 1
                    try:
                        yield
                    finally:
                        self._local.depth = 0

    @contextmanager
    def read(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self):
        with self.lock(), self.read() as db:
            try:
                db.execute("PRAGMA foreign_keys=ON")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def audit(self, db, action: str, actor: str, target: str, details=None):
        db.execute(
            "INSERT INTO audit(time,action,actor,target,details) VALUES(?,?,?,?,?)",
            (now(), action, actor, target, dumps(details or {})),
        )
