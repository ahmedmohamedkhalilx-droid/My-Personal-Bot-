"""Tiny SQLite store: remembers which emails / Canvas items were already reported."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS seen (kind TEXT, key TEXT, PRIMARY KEY (kind, key))")
            self._conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")

    def is_seen(self, kind: str, key: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM seen WHERE kind=? AND key=?", (kind, str(key))).fetchone()
        return row is not None

    def mark_seen(self, kind: str, key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO seen (kind, key) VALUES (?, ?)", (kind, str(key)))

    def has_any(self, kind: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM seen WHERE kind=? LIMIT 1", (kind,)).fetchone()
        return row is not None

    def is_baselined(self, kind: str) -> bool:
        return self.get(f"baseline:{kind}") is not None

    def set_baselined(self, kind: str) -> None:
        self.set(f"baseline:{kind}", True)

    def get(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))
