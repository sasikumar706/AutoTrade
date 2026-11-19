"""Tiny SQLite store: key/value (tokens), saved backtests, live/paper session logs."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class Store:
    def backup(self, dest) -> None:
        """Consistent copy of the whole database (safe while the server runs)."""
        import sqlite3 as _sq
        with self._lock:
            out = _sq.connect(str(dest))
            try:
                self._db.backup(out)
            finally:
                out.close()

    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS backtests (
                id TEXT PRIMARY KEY, created REAL, title TEXT, summary TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS session_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts REAL, kind TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY, created REAL, title TEXT, summary TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, created REAL, config TEXT, status TEXT, summary TEXT);
            """
        )
        self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    # key/value
    def get(self, k: str, default: Any = None) -> Any:
        rows = self._exec("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(rows[0][0]) if rows else default

    def set(self, k: str, v: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v, default=str)))

    def items(self, prefix: str) -> list[tuple[str, Any]]:
        rows = self._exec("SELECT k, v FROM kv WHERE k LIKE ? ORDER BY k", (prefix + "%",))
        return [(r[0], json.loads(r[1])) for r in rows]

    def delete(self, k: str) -> None:
        self._exec("DELETE FROM kv WHERE k=?", (k,))

    # backtests
    def save_backtest(self, title: str, result: dict) -> str:
        bid = uuid.uuid4().hex[:12]
        self._exec(
            "INSERT INTO backtests VALUES (?,?,?,?,?)",
            (bid, time.time(), title, json.dumps(result["metrics"]), json.dumps(result, default=str)),
        )
        return bid

    def list_backtests(self, limit: int = 50) -> list[dict]:
        rows = self._exec("SELECT id, created, title, summary FROM backtests ORDER BY created DESC LIMIT ?", (limit,))
        return [{"id": r[0], "created": r[1], "title": r[2], "metrics": json.loads(r[3])} for r in rows]

    def get_backtest(self, bid: str) -> dict | None:
        rows = self._exec("SELECT result FROM backtests WHERE id=?", (bid,))
        return json.loads(rows[0][0]) if rows else None

    def delete_backtest(self, bid: str) -> None:
        self._exec("DELETE FROM backtests WHERE id=?", (bid,))

    # AI analyses (history the user can keep or clear)
    def save_analysis(self, title: str, summary: dict, result: dict, keep: int = 50) -> tuple[str, float]:
        aid, now = uuid.uuid4().hex[:12], time.time()
        self._exec(
            "INSERT INTO analyses VALUES (?,?,?,?,?)",
            (aid, now, title, json.dumps(summary, default=str), json.dumps(result, default=str)),
        )
        self._exec(
            "DELETE FROM analyses WHERE id NOT IN (SELECT id FROM analyses ORDER BY created DESC LIMIT ?)", (keep,)
        )
        return aid, now

    def list_analyses(self, limit: int = 50) -> list[dict]:
        rows = self._exec("SELECT id, created, title, summary FROM analyses ORDER BY created DESC LIMIT ?", (limit,))
        return [{"id": r[0], "created": r[1], "title": r[2], **json.loads(r[3])} for r in rows]

    def get_analysis(self, aid: str) -> dict | None:
        rows = self._exec("SELECT result FROM analyses WHERE id=?", (aid,))
        return json.loads(rows[0][0]) if rows else None

    def delete_analysis(self, aid: str | None = None) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM analyses WHERE id=?", (aid,)) if aid else self._db.execute("DELETE FROM analyses")
            self._db.commit()
            return cur.rowcount

    # sessions
    def upsert_session(self, sid: str, config: dict, status: str, summary: dict) -> None:
        self._exec(
            "INSERT INTO sessions (id, created, config, status, summary) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, summary=excluded.summary",
            (sid, time.time(), json.dumps(config, default=str), status, json.dumps(summary, default=str)),
        )

    def list_sessions(self, limit: int = 50) -> list[dict]:
        rows = self._exec("SELECT id, created, config, status, summary FROM sessions ORDER BY created DESC LIMIT ?", (limit,))
        return [
            {"id": r[0], "created": r[1], "config": json.loads(r[2]), "status": r[3], "summary": json.loads(r[4])}
            for r in rows
        ]

    def delete_sessions(self, modes: tuple[str, ...], keep: set[str]) -> int:
        """Delete finished sessions of the given modes (and their event logs); ids in keep are left alone."""
        n = 0
        for row in self.list_sessions(limit=100_000):
            if row["id"] in keep or row["config"].get("mode") not in modes:
                continue
            self._exec("DELETE FROM sessions WHERE id=?", (row["id"],))
            self._exec("DELETE FROM session_events WHERE session_id=?", (row["id"],))
            n += 1
        return n

    def log_event(self, sid: str, kind: str, data: dict) -> None:
        self._exec(
            "INSERT INTO session_events (session_id, ts, kind, data) VALUES (?,?,?,?)",
            (sid, time.time(), kind, json.dumps(data, default=str)),
        )

    def events(self, sid: str, limit: int = 200) -> list[dict]:
        rows = self._exec(
            "SELECT ts, kind, data FROM session_events WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, limit)
        )
        return [{"ts": r[0], "kind": r[1], "data": json.loads(r[2])} for r in rows]
