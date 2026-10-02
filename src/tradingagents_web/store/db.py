"""One SQLite file holds every job, its event stream and the saved settings.

The API process and each worker process open their own connection; a
connection is never shared across processes. WAL mode lets the API read while
a worker writes, and the busy timeout absorbs the brief write locks.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,              -- analysis | backtest | settle
    status        TEXT NOT NULL,              -- queued | running | completed | failed | cancelled
    ticker        TEXT,                       -- analysis and settle: the ticker
    trade_date    TEXT,                       -- analysis: the analysis date
    tickers       TEXT NOT NULL,              -- JSON list of tickers the job locks
    request       TEXT NOT NULL,              -- JSON request, portfolio snapshot included
    settings      TEXT,                       -- JSON run_settings() of the graph that ran
    rating        TEXT,
    report_dir    TEXT,
    result        TEXT,                       -- JSON: report sections, backtest summary
    resumable     INTEGER NOT NULL DEFAULT 0,
    resumed_from  INTEGER,
    error         TEXT,
    progress_done INTEGER,
    progress_total INTEGER,
    pid           INTEGER,
    source        TEXT NOT NULL DEFAULT 'web', -- web | cli (a report imported from the CLI) | schedule
    schedule_id   INTEGER,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_kind_ticker ON jobs (kind, ticker);

CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    tickers      TEXT NOT NULL,               -- JSON list
    days         TEXT NOT NULL,               -- JSON list of weekdays, 0 = Monday
    time         TEXT NOT NULL,               -- HH:MM in the server's time zone
    request      TEXT NOT NULL,               -- JSON: the run choices, resolved when it fires
    enabled      INTEGER NOT NULL DEFAULT 1,
    next_run_at  TEXT,
    last_run_at  TEXT,
    last_jobs    TEXT,                        -- JSON list of job ids from the last firing
    last_error   TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_events (
    job_id  INTEGER NOT NULL,
    seq     INTEGER NOT NULL,
    type    TEXT NOT NULL,
    data    TEXT NOT NULL,
    ts      TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

JSON_COLUMNS = ("tickers", "request", "settings", "result")
SCHEDULE_JSON = ("tickers", "days", "request", "last_jobs")

# Columns added after the first release, for databases created before them.
MIGRATIONS = {
    "jobs": [("source", "TEXT NOT NULL DEFAULT 'web'"), ("schedule_id", "INTEGER")],
}
ACTIVE = ("queued", "running")
FINISHED = ("completed", "failed", "cancelled")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    job = dict(row)
    for key in JSON_COLUMNS:
        if job.get(key) is not None:
            job[key] = json.loads(job[key])
    job["resumable"] = bool(job.get("resumable"))
    return job


class Store:
    """Typed access to the web database over one connection."""

    def __init__(self, path: str | Path, *, init: bool = True):
        self.path = Path(path)
        self.conn = connect(self.path)
        if init:
            self._migrate()
            self.conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue        # a new database: SCHEMA creates the table whole
            for name, decl in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    # --- jobs -------------------------------------------------------------

    def create_job(self, kind: str, request: dict, tickers: list[str], *, ticker: str | None = None,
                   trade_date: str | None = None, resumed_from: int | None = None,
                   source: str = "web", schedule_id: int | None = None,
                   status: str = "queued") -> int:
        cur = self.conn.execute(
            "INSERT INTO jobs (kind, status, ticker, trade_date, tickers, request, resumed_from,"
            " source, schedule_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, status, ticker, trade_date, json.dumps(tickers), json.dumps(request), resumed_from,
             source, schedule_id, now()),
        )
        return int(cur.lastrowid)

    def delete_job(self, job_id: int) -> None:
        """Remove a job and its events; a job resumed from it keeps no dangling link."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM job_events WHERE job_id = ?", (job_id,))
            self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            self.conn.execute("UPDATE jobs SET resumed_from = NULL WHERE resumed_from = ?", (job_id,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def report_dirs(self) -> set[str]:
        return {r["report_dir"] for r in self.conn.execute(
            "SELECT report_dir FROM jobs WHERE report_dir IS NOT NULL")}

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return _row(self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def update_job(self, job_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in JSON_COLUMNS:
            if key in fields and fields[key] is not None:
                fields[key] = json.dumps(fields[key])
        if "resumable" in fields:
            fields["resumable"] = int(bool(fields["resumable"]))
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def transition(self, job_id: int, from_status: tuple[str, ...], **fields: Any) -> bool:
        """Update the job only while it is in one of ``from_status``; True when it was.

        The worker and the manager both finish jobs, so each guards on the
        status it expects: a cancel that lands first is never overwritten.
        """
        for key in JSON_COLUMNS:
            if key in fields and fields[key] is not None:
                fields[key] = json.dumps(fields[key])
        if "resumable" in fields:
            fields["resumable"] = int(bool(fields["resumable"]))
        cols = ", ".join(f"{k} = ?" for k in fields)
        marks = ", ".join("?" for _ in from_status)
        cur = self.conn.execute(
            f"UPDATE jobs SET {cols} WHERE id = ? AND status IN ({marks})",
            (*fields.values(), job_id, *from_status),
        )
        return cur.rowcount == 1

    def list_jobs(self, *, kind: str | None = None, status: str | list[str] | None = None,
                  ticker: str | None = None, rating: str | None = None,
                  schedule_id: int | None = None, ids: list[int] | None = None,
                  limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        where, args = [], []
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if schedule_id is not None:
            where.append("schedule_id = ?")
            args.append(schedule_id)
        if ids is not None:
            where.append(f"id IN ({', '.join('?' for _ in ids) or 'NULL'})")
            args.extend(ids)
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            args.extend(statuses)
        if ticker:
            where.append("ticker = ?")
            args.append(ticker)
        if rating:
            where.append("rating = ?")
            args.append(rating)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        rows = self.conn.execute(sql, (*args, limit, offset)).fetchall()
        return [_row(r) for r in rows]

    def queued_jobs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id").fetchall()
        return [_row(r) for r in rows]

    def running_jobs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM jobs WHERE status = 'running' ORDER BY id").fetchall()
        return [_row(r) for r in rows]

    # --- events -----------------------------------------------------------

    def append_event(self, job_id: int, type_: str, data: dict | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO job_events (job_id, seq, type, data, ts) "
            "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM job_events WHERE job_id = ? "
            "RETURNING seq",
            (job_id, type_, json.dumps(data or {}), now(), job_id),
        )
        return int(cur.fetchone()[0])

    def events(self, job_id: int, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT seq, type, data, ts FROM job_events WHERE job_id = ? AND seq > ? ORDER BY seq"
        args: tuple = (job_id, after)
        if limit:
            sql += " LIMIT ?"
            args = (*args, limit)
        return [
            {"seq": r["seq"], "type": r["type"], "data": json.loads(r["data"]), "ts": r["ts"]}
            for r in self.conn.execute(sql, args).fetchall()
        ]

    # --- key/value --------------------------------------------------------

    def get_value(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_value(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    # --- schedules --------------------------------------------------------

    @staticmethod
    def _schedule(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        schedule = dict(row)
        for key in SCHEDULE_JSON:
            if schedule.get(key) is not None:
                schedule[key] = json.loads(schedule[key])
        schedule["enabled"] = bool(schedule["enabled"])
        return schedule

    def create_schedule(self, *, name: str, tickers: list[str], days: list[int], time: str,
                        request: dict, enabled: bool, next_run_at: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO schedules (name, tickers, days, time, request, enabled, next_run_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, json.dumps(tickers), json.dumps(days), time, json.dumps(request), int(enabled),
             next_run_at, now()),
        )
        return int(cur.lastrowid)

    def update_schedule(self, schedule_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in SCHEDULE_JSON:
            if key in fields and fields[key] is not None:
                fields[key] = json.dumps(fields[key])
        if "enabled" in fields:
            fields["enabled"] = int(bool(fields["enabled"]))
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE schedules SET {cols} WHERE id = ?", (*fields.values(), schedule_id))

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        return self._schedule(self.conn.execute("SELECT * FROM schedules WHERE id = ?",
                                                (schedule_id,)).fetchone())

    def list_schedules(self) -> list[dict[str, Any]]:
        return [self._schedule(r) for r in self.conn.execute("SELECT * FROM schedules ORDER BY id")]

    def due_schedules(self, moment: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?"
            " ORDER BY next_run_at", (moment,))
        return [self._schedule(r) for r in rows]

    def delete_schedule(self, schedule_id: int) -> None:
        self.conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
