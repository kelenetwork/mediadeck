"""SQLite storage for operational data.

The settings store (a JSON document) is right for configuration: small, read
often, written rarely, and worth having as a human-readable file.  It is wrong
for usage data.  Traffic accounting appends thousands of rows a day and has to
answer aggregate questions ("how much did this user watch in August"), and a
JSON blob rewritten on every sample would both lose concurrent writes and grow
without bound.

So: configuration stays in JSON, usage and membership live here.

WAL is enabled because the panel reads (dashboard, stats) while a background
sampler writes; without it those readers would block the writer and the UI
would stall behind accounting.
"""
from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
-- A plan is a template: the limits and billing rules a member inherits.
CREATE TABLE IF NOT EXISTS plans (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    billing_type        TEXT NOT NULL DEFAULT 'unlimited',
    traffic_quota_bytes INTEGER NOT NULL DEFAULT 0,
    traffic_period      TEXT NOT NULL DEFAULT 'monthly',
    duration_days       INTEGER NOT NULL DEFAULT 0,
    max_streams         INTEGER NOT NULL DEFAULT 1,
    max_bitrate_kbps    INTEGER NOT NULL DEFAULT 0,
    max_devices         INTEGER NOT NULL DEFAULT 0,
    allow_transcode     INTEGER NOT NULL DEFAULT 0,
    allow_download      INTEGER NOT NULL DEFAULT 0,
    allow_sync          INTEGER NOT NULL DEFAULT 0,
    libraries_json      TEXT NOT NULL DEFAULT '[]',
    price_cents         INTEGER NOT NULL DEFAULT 0,
    currency            TEXT NOT NULL DEFAULT 'CNY',
    priority            INTEGER NOT NULL DEFAULT 0,
    is_default          INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);

-- A member links an Emby user to a plan. Emby users with no row here are
-- deliberately invisible to enforcement: this server has hundreds of accounts
-- that predate the panel, and a bug must never be able to disable them.
CREATE TABLE IF NOT EXISTS members (
    emby_user_id        TEXT PRIMARY KEY,
    username            TEXT NOT NULL DEFAULT '',
    plan_id             TEXT,
    status              TEXT NOT NULL DEFAULT 'active',
    expires_at          INTEGER,
    traffic_used_bytes  INTEGER NOT NULL DEFAULT 0,
    traffic_period_start INTEGER NOT NULL DEFAULT 0,
    note                TEXT NOT NULL DEFAULT '',
    contact             TEXT NOT NULL DEFAULT '',
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    last_seen_at        INTEGER,
    -- Snapshot of what was last pushed into Emby, so the reconciler can tell
    -- "nothing changed" from "never applied" without re-writing every policy
    -- on every pass.
    applied_fingerprint TEXT NOT NULL DEFAULT '',
    applied_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_members_plan ON members(plan_id);
CREATE INDEX IF NOT EXISTS idx_members_status ON members(status);

-- One row per device per member. Emby has a device list but no notion of
-- "this account may use at most N devices", so the limit is enforced here.
CREATE TABLE IF NOT EXISTS devices (
    emby_user_id    TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    device_name     TEXT NOT NULL DEFAULT '',
    client          TEXT NOT NULL DEFAULT '',
    app_version     TEXT NOT NULL DEFAULT '',
    last_ip         TEXT NOT NULL DEFAULT '',
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    blocked         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (emby_user_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(emby_user_id);
CREATE INDEX IF NOT EXISTS idx_devices_seen ON devices(last_seen_at);

-- Rolled up per user per day. Sampling writes here continuously, so it is kept
-- narrow and indexed for the two questions actually asked: one user's history,
-- and one day across all users.
CREATE TABLE IF NOT EXISTS usage_daily (
    day             TEXT NOT NULL,
    emby_user_id    TEXT NOT NULL,
    bytes           INTEGER NOT NULL DEFAULT 0,
    seconds         INTEGER NOT NULL DEFAULT 0,
    plays           INTEGER NOT NULL DEFAULT 0,
    transcodes      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, emby_user_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_daily(day);

-- Finished playback sessions: the source for "top titles", "what did this user
-- watch", and per-node traffic attribution.
CREATE TABLE IF NOT EXISTS play_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    emby_user_id    TEXT NOT NULL,
    username        TEXT NOT NULL DEFAULT '',
    item_id         TEXT NOT NULL DEFAULT '',
    item_name       TEXT NOT NULL DEFAULT '',
    item_type       TEXT NOT NULL DEFAULT '',
    series_name     TEXT NOT NULL DEFAULT '',
    device_id       TEXT NOT NULL DEFAULT '',
    client          TEXT NOT NULL DEFAULT '',
    play_method     TEXT NOT NULL DEFAULT '',
    node            TEXT NOT NULL DEFAULT '',
    remote_ip       TEXT NOT NULL DEFAULT '',
    bytes           INTEGER NOT NULL DEFAULT 0,
    seconds         INTEGER NOT NULL DEFAULT 0,
    started_at      INTEGER NOT NULL,
    ended_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_play_user ON play_events(emby_user_id);
CREATE INDEX IF NOT EXISTS idx_play_started ON play_events(started_at);
CREATE INDEX IF NOT EXISTS idx_play_item ON play_events(item_id);

-- Every enforcement action, so "why was this account disabled" always has an
-- answer. Billing disputes are unanswerable without this.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system',
    action      TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_subject ON audit_log(subject);

-- Invitation codes: how a new member is created without the operator handing
-- out passwords by hand.
CREATE TABLE IF NOT EXISTS invites (
    code            TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER,
    max_uses        INTEGER NOT NULL DEFAULT 1,
    used_count      INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL DEFAULT '',
    revoked         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # One connection guarded by a lock rather than a pool: writes are
        # low-volume and serialising them avoids SQLITE_BUSY entirely, which is
        # far easier to reason about than retry loops.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False,
                                     timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @property
    def path(self) -> Path:
        return self._path

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            cur = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'")
            row = cur.fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
            self._conn.commit()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Transactional write; rolls back on error so a failed enforcement
        pass cannot leave half-applied state."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.write() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
