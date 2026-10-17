"""Local validated analysis cache and a conservative free request ledger."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from llm_provider import ProviderError, _cancel, _loads


VERSION = 1
_KEY = re.compile(r"[a-f0-9]{64}\Z")
_COLUMNS = {
    "chunks": {"key", "payload", "created_at"},
    "attempts": {"id", "day", "started_at", "request_key", "status", "usage"},
}


class AnalysisStore:
    def __init__(self, path, *, clock=None, sleep=None, interval=3.1):
        self.path = Path(path)
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep
        self.interval = float(interval)
        if not math.isfinite(self.interval) or self.interval < 0:
            raise ValueError("The analysis pacing interval must be finite and nonnegative.")
        self.interval = max(3.0, self.interval)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            objects = list(connection.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            tables = {row["name"] for row in objects if row["type"] == "table"}
            if version == 0 and not tables:
                if objects:
                    raise ValueError("The local analysis database has an unsupported format.")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE chunks (key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)")
                connection.execute("CREATE TABLE attempts (id TEXT PRIMARY KEY, day TEXT NOT NULL, started_at REAL NOT NULL, request_key TEXT NOT NULL, status TEXT NOT NULL, usage TEXT NOT NULL)")
                connection.execute("PRAGMA user_version=1")
            else:
                if version != VERSION or tables != set(_COLUMNS) or any(row["type"] != "table" for row in objects):
                    raise ValueError("The local analysis database has an unsupported format.")
                for table, required in _COLUMNS.items():
                    columns = list(connection.execute(f"PRAGMA table_xinfo({table})"))
                    if {row["name"] for row in columns} != required:
                        raise ValueError("The local analysis database has an incompatible schema.")
                    for row in columns:
                        expected = "REAL" if row["name"] in {"created_at", "started_at"} else "TEXT"
                        if row["hidden"] or row["type"].upper() != expected or (not row["pk"] and not row["notnull"]):
                            raise ValueError("The local analysis database has incompatible column types.")
                    primary = [row["name"] for row in sorted(columns, key=lambda item: item["pk"]) if row["pk"]]
                    if primary != (["key"] if table == "chunks" else ["id"]):
                        raise ValueError("The local analysis database has an incompatible primary key.")
                    if list(connection.execute(f"PRAGMA foreign_key_list({table})")):
                        raise ValueError("The local analysis database has incompatible table relations.")

    @staticmethod
    def _validate_key(key):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise ValueError("Invalid analysis cache identity.")

    def get_chunk(self, key):
        self._validate_key(key)
        with self._connection() as connection:
            row = connection.execute("SELECT payload FROM chunks WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if len(row["payload"]) > 1024 * 1024:
            raise ValueError("The local analysis cache is invalid.")
        try:
            value = _loads(row["payload"])
        except (ValueError, RecursionError):
            raise ValueError("The local analysis cache is invalid.") from None
        if not isinstance(value, dict):
            raise ValueError("The local analysis cache is invalid.")
        return value

    def put_chunk(self, key, payload):
        self._validate_key(key)
        if not isinstance(payload, dict):
            raise ValueError("Only validated analysis objects can be cached.")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("The analysis cache entry is too large.")
        with self._connection() as connection:
            connection.execute("INSERT INTO chunks VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,created_at=excluded.created_at",
                               (key, encoded, self.clock()))

    def daily_remaining(self):
        day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        with self._connection() as connection:
            used = connection.execute("SELECT COUNT(*) FROM attempts WHERE day=?", (day,)).fetchone()[0]
        return max(0, 50 - used)

    def reserve_attempt(self, key, *, cancelled=None):
        self._validate_key(key)
        while True:
            _cancel(cancelled)
            with self._lock, self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self.clock()
                date = datetime.fromtimestamp(current, timezone.utc)
                day = date.date().isoformat()
                used = connection.execute("SELECT COUNT(*) FROM attempts WHERE day=?", (day,)).fetchone()[0]
                if used >= 50:
                    seconds = 86400 - (date.hour * 3600 + date.minute * 60 + date.second)
                    raise ProviderError("The local free request budget is used for today. Resume after the UTC day resets.", code="daily_limit", retry_after=seconds)
                last = connection.execute("SELECT MAX(started_at) FROM attempts").fetchone()[0]
                wait = max(0.0, self.interval - (current - last)) if last is not None else 0.0
                if wait <= 0:
                    identity = uuid.uuid4().hex
                    connection.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?)", (identity, day, current, key, "reserved", "{}"))
                    return identity
            self.sleep(min(wait, 0.1))

    def finish_attempt(self, identity, *, success=False, usage=None):
        safe_usage = usage or {}
        encoded = json.dumps(safe_usage, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 4096:
            raise ValueError("The analysis usage record is too large.")
        with self._connection() as connection:
            connection.execute("UPDATE attempts SET status=?,usage=? WHERE id=?", ("succeeded" if success else "failed", encoded, identity))

    def close(self):
        """Connections are scoped to individual operations."""

