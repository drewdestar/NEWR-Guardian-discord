"""SQLite persistence for NEWR Guardian. No secrets are stored here.

Pattern copied from newr-stats/db.py and newr-social/db.py: one
module-level DB_PATH (env DB_PATH, else data.db next to this file),
a _conn() helper with Row factory, and init_db() creating every
table with IF NOT EXISTS.

Tables:
  settings   (guild_id, key, value) — per-guild config.
               Keys: log_channel, threshold_count (default 3),
               threshold_window (seconds, default 60),
               raidmode ("1"/"0", default "0"),
               lockdown_active ("1"/"0", default "0").
  whitelist  (guild_id, user_id, added_by, added_at) — members
               exempt from anti-nuke detection, in addition to the
               guild owner and staff (who are exempt by default).
  incidents  (id, guild_id, attacker_id, summary, count, action,
               created_at) — auto-lockdown / panic / raid-burst log.
               This is the durable log; /guard log reads it. The
               #logs channel post is a second, human-readable copy.

DB_PATH env var overrides the default data.db next to this file.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

DEFAULT_THRESHOLD_COUNT = 3
DEFAULT_THRESHOLD_WINDOW = 60


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings(
            guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (guild_id, key));
        CREATE TABLE IF NOT EXISTS whitelist(
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            added_by INTEGER NOT NULL, added_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id));
        CREATE TABLE IF NOT EXISTS incidents(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL, attacker_id INTEGER,
            summary TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            action TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL);
        """)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Settings -----------------------------------------------------------

def get_setting(guild_id: int, key: str, default: str | None = None) -> str | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE guild_id=? AND key=?",
            (guild_id, key)).fetchone()
    return row["value"] if row is not None else default


def set_setting(guild_id: int, key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO settings VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
            (guild_id, key, value))


def get_int_setting(guild_id: int, key: str, default: int) -> int:
    raw = get_setting(guild_id, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def threshold(guild_id: int) -> tuple[int, int]:
    """(count, window_seconds) for mass-action detection."""
    count = get_int_setting(guild_id, "threshold_count", DEFAULT_THRESHOLD_COUNT)
    window = get_int_setting(guild_id, "threshold_window", DEFAULT_THRESHOLD_WINDOW)
    return max(1, min(20, count)), max(10, min(600, window))


def raidmode_enabled(guild_id: int) -> bool:
    return get_setting(guild_id, "raidmode", "0") == "1"


def lockdown_active(guild_id: int) -> bool:
    return get_setting(guild_id, "lockdown_active", "0") == "1"


def set_lockdown_active(guild_id: int, active: bool) -> None:
    set_setting(guild_id, "lockdown_active", "1" if active else "0")


# --- Whitelist ------------------------------------------------------------

def whitelist_add(guild_id: int, user_id: int, added_by: int) -> bool:
    """Returns False if the member was already whitelisted."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO whitelist VALUES (?, ?, ?, ?)",
            (guild_id, user_id, added_by, now()))
        return cur.rowcount > 0


def whitelist_remove(guild_id: int, user_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM whitelist WHERE guild_id=? AND user_id=?",
            (guild_id, user_id))
        return cur.rowcount > 0


def whitelist_list(guild_id: int) -> list[int]:
    with _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT user_id FROM whitelist WHERE guild_id=? ORDER BY user_id ASC",
            (guild_id,)).fetchall()]


def is_whitelisted(guild_id: int, user_id: int) -> bool:
    with _conn() as conn:
        return conn.execute(
            "SELECT 1 FROM whitelist WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)).fetchone() is not None


# --- Incidents ------------------------------------------------------------

def log_incident(guild_id: int, attacker_id: int | None, summary: str,
                 count: int, action: str) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO incidents (guild_id, attacker_id, summary, count, action, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, attacker_id, summary, count, action, now()))
        return cur.lastrowid


def recent_incidents(guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM incidents WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, limit)).fetchall()
