"""Durable state with a process-local lock per database and SQLite transactions."""
import json
import sqlite3
import threading
from pathlib import Path

_LOCKS = {}
_GUARD = threading.Lock()


class RuntimeStore:
    def __init__(self, path: str):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        identity = str(Path(path).resolve()) if path != ':memory:' else id(self)
        with _GUARD:
            self.lock = _LOCKS.setdefault(identity, threading.RLock())
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        with self.lock:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            self.db.commit()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value, allow_nan=False)))
