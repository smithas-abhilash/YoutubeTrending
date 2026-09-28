"""Local SQLite store: API cache, quota log, trending snapshots and history.

Everything lives in data/ytrend.db next to the app, so nothing leaves your PC.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "ytrend.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_cache (
    key TEXT PRIMARY KEY, ts REAL NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS quota_log (
    ts REAL NOT NULL, day TEXT NOT NULL, resource TEXT NOT NULL, units INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL,
    regions TEXT NOT NULL, rows TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_quota_day ON quota_log(day);
CREATE INDEX IF NOT EXISTS idx_history_kind ON history(kind, ts);
"""


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(_SCHEMA)
    return conn


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- API cache
def cache_get(key: str, ttl_s: float) -> Any | None:
    with _conn() as c:
        row = c.execute("SELECT ts, body FROM api_cache WHERE key = ?", (key,)).fetchone()
    if row and time.time() - row[0] < ttl_s:
        return json.loads(row[1])
    return None


def cache_put(key: str, body: Any) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO api_cache VALUES (?, ?, ?)", (key, time.time(), _dumps(body)))


def cache_clear() -> None:
    with _conn() as c:
        c.execute("DELETE FROM api_cache")


# ---------------------------------------------------------------- quota
def _quota_day() -> str:
    # YouTube quota resets at midnight Pacific time (UTC-8, ignoring DST is close enough).
    return (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")


def quota_add(resource: str, units: int) -> None:
    with _conn() as c:
        c.execute("INSERT INTO quota_log VALUES (?, ?, ?, ?)", (time.time(), _quota_day(), resource, units))


def quota_used_today() -> int:
    with _conn() as c:
        row = c.execute("SELECT COALESCE(SUM(units), 0) FROM quota_log WHERE day = ?", (_quota_day(),)).fetchone()
    return int(row[0])


# ---------------------------------------------------------------- trending snapshots
def snapshot_save(regions: list[str], rows: list[dict]) -> None:
    slim = [
        {k: r.get(k) for k in ("video_id", "channel_id", "channel", "language", "category",
                               "style", "format", "views", "views_per_hour")}
        for r in rows
    ]
    with _conn() as c:
        c.execute(
            "INSERT INTO snapshots (ts, day, regions, rows) VALUES (?, ?, ?, ?)",
            (time.time(), datetime.now().strftime("%Y-%m-%d"), ",".join(sorted(regions)), _dumps(slim)),
        )


def snapshots_load(regions: list[str], days: int = 30) -> list[dict]:
    """Latest snapshot per day for this region set, oldest first."""
    since = time.time() - days * 86400
    with _conn() as c:
        rows = c.execute(
            "SELECT day, rows FROM snapshots WHERE regions = ? AND ts >= ? ORDER BY ts",
            (",".join(sorted(regions)), since),
        ).fetchall()
    per_day: dict[str, list[dict]] = {}
    for day, body in rows:
        per_day[day] = json.loads(body)  # later snapshots of the same day win
    return [{"day": d, "rows": r} for d, r in per_day.items()]


# ---------------------------------------------------------------- history
def history_save(kind: str, title: str, body: Any) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO history (ts, kind, title, body) VALUES (?, ?, ?, ?)",
            (time.time(), kind, title, _dumps(body)),
        )
        return int(cur.lastrowid)


def history_list(kind: str | None = None, limit: int = 100) -> list[dict]:
    q = "SELECT id, ts, kind, title FROM history"
    args: tuple = ()
    if kind:
        q += " WHERE kind = ?"
        args = (kind,)
    q += " ORDER BY ts DESC LIMIT ?"
    with _conn() as c:
        rows = c.execute(q, args + (limit,)).fetchall()
    return [{"id": i, "when": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"), "kind": k, "title": t}
            for i, ts, k, t in rows]


def history_get(item_id: int) -> Any | None:
    with _conn() as c:
        row = c.execute("SELECT body FROM history WHERE id = ?", (item_id,)).fetchone()
    return json.loads(row[0]) if row else None


def history_delete(item_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM history WHERE id = ?", (item_id,))
