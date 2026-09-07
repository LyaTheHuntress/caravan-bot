"""
Simple SQLite data layer for the caravan bot.
Handles members, the conductor queue, the VIP queue, and the history log.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "caravan.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS members (
            discord_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            last_conducted_at TEXT,
            last_vip_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS conductor_queue (
            discord_id TEXT PRIMARY KEY,
            preferred_time TEXT,
            joined_at TEXT NOT NULL,
            notified_at TEXT,
            FOREIGN KEY (discord_id) REFERENCES members (discord_id)
        )
    """)

    # Migration: add notified_at to any pre-existing conductor_queue table that predates this column
    existing_cols = [row["name"] for row in cur.execute("PRAGMA table_info(conductor_queue)").fetchall()]
    if "notified_at" not in existing_cols:
        cur.execute("ALTER TABLE conductor_queue ADD COLUMN notified_at TEXT")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_queue (
            discord_id TEXT PRIMARY KEY,
            joined_at TEXT NOT NULL,
            FOREIGN KEY (discord_id) REFERENCES members (discord_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS assignment_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('conductor', 'vip')),
            timestamp TEXT NOT NULL,
            assigned_by TEXT,
            is_backfill INTEGER NOT NULL DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_member(discord_id: str, name: str):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO members (discord_id, name) VALUES (?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET name = excluded.name
        """,
        (discord_id, name),
    )
    conn.commit()
    conn.close()


# ---------- Conductor queue ----------

def join_conductor_queue(discord_id: str, name: str, preferred_time: Optional[str]):
    ensure_member(discord_id, name)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO conductor_queue (discord_id, preferred_time, joined_at, notified_at)
        VALUES (?, ?, ?, NULL)
        ON CONFLICT(discord_id) DO UPDATE SET
            preferred_time = excluded.preferred_time,
            joined_at = excluded.joined_at,
            notified_at = NULL
        """,
        (discord_id, preferred_time, now_iso()),
    )
    conn.commit()
    conn.close()


def leave_conductor_queue(discord_id: str):
    conn = get_connection()
    conn.execute("DELETE FROM conductor_queue WHERE discord_id = ?", (discord_id,))
    conn.commit()
    conn.close()


def get_conductor_queue():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT cq.discord_id, m.name, cq.preferred_time, cq.joined_at, m.last_conducted_at
        FROM conductor_queue cq
        JOIN members m ON m.discord_id = cq.discord_id
        ORDER BY cq.preferred_time IS NULL, cq.preferred_time ASC
        """
    ).fetchall()
    conn.close()
    return rows


def get_due_conductor_entries(current_time_str: str):
    """Queue entries whose preferred_time matches the given 'HH:MM' string and haven't been notified yet."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT cq.discord_id, m.name, cq.preferred_time
        FROM conductor_queue cq
        JOIN members m ON m.discord_id = cq.discord_id
        WHERE cq.preferred_time = ? AND cq.notified_at IS NULL
        """,
        (current_time_str,),
    ).fetchall()
    conn.close()
    return rows


def get_pending_notification_entries():
    """All conductor queue entries that have a preferred_time set.
    Includes notified_at (for per-day dedupe) and last_conducted_at (fairness tie-break).
    This list is a persistent rotation — entries are never removed just for having
    been notified; the caller decides whether today's notification already fired."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT cq.discord_id, m.name, cq.preferred_time, cq.notified_at, m.last_conducted_at
        FROM conductor_queue cq
        JOIN members m ON m.discord_id = cq.discord_id
        WHERE cq.preferred_time IS NOT NULL
        """
    ).fetchall()
    conn.close()
    return rows


def mark_conductor_notified(discord_id: str, when_iso: Optional[str] = None):
    conn = get_connection()
    conn.execute(
        "UPDATE conductor_queue SET notified_at = ? WHERE discord_id = ?",
        (when_iso or now_iso(), discord_id),
    )
    conn.commit()
    conn.close()


def get_next_vip(exclude_discord_id: Optional[str] = None):
    """Whoever on the VIP list has waited longest since their last VIP turn
    (never-VIP members first). Pass exclude_discord_id to skip a specific
    member (e.g. today's conductor). This list is a persistent rotation —
    being picked does not remove anyone from it."""
    conn = get_connection()
    query = """
        SELECT vq.discord_id, m.name, m.last_vip_at
        FROM vip_queue vq
        JOIN members m ON m.discord_id = vq.discord_id
    """
    params = ()
    if exclude_discord_id:
        query += " WHERE vq.discord_id != ?"
        params = (exclude_discord_id,)
    query += " ORDER BY m.last_vip_at IS NOT NULL, m.last_vip_at ASC"
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row


# ---------- VIP queue ----------

def join_vip_queue(discord_id: str, name: str):
    ensure_member(discord_id, name)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO vip_queue (discord_id, joined_at) VALUES (?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET joined_at = excluded.joined_at
        """,
        (discord_id, now_iso()),
    )
    conn.commit()
    conn.close()


def leave_vip_queue(discord_id: str):
    conn = get_connection()
    conn.execute("DELETE FROM vip_queue WHERE discord_id = ?", (discord_id,))
    conn.commit()
    conn.close()


def get_vip_queue():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT vq.discord_id, m.name, vq.joined_at, m.last_vip_at
        FROM vip_queue vq
        JOIN members m ON m.discord_id = vq.discord_id
        ORDER BY vq.joined_at ASC
        """
    ).fetchall()
    conn.close()
    return rows


# ---------- Assignment (live, starts countdown) ----------

def assign_conductor(discord_id: str, name: str, assigned_by: str):
    """Record this member as conductor for the current run. They stay on the
    conductor list with their preferred time — it's a persistent rotation,
    not a one-time queue, so no one needs to re-join after their turn."""
    ensure_member(discord_id, name)
    ts = now_iso()
    conn = get_connection()
    conn.execute(
        "UPDATE members SET last_conducted_at = ? WHERE discord_id = ?",
        (ts, discord_id),
    )
    conn.execute(
        "UPDATE conductor_queue SET notified_at = ? WHERE discord_id = ?",
        (ts, discord_id),
    )
    conn.execute(
        """
        INSERT INTO assignment_log (discord_id, role, timestamp, assigned_by, is_backfill)
        VALUES (?, 'conductor', ?, ?, 0)
        """,
        (discord_id, ts, assigned_by),
    )
    conn.commit()
    conn.close()
    return ts


def assign_vip(discord_id: str, name: str, assigned_by: str):
    """Record this member as VIP for the current run. They stay on the VIP
    list — it's a persistent rotation, not a one-time queue, so no one needs
    to re-join after their turn."""
    ensure_member(discord_id, name)
    ts = now_iso()
    conn = get_connection()
    conn.execute(
        "UPDATE members SET last_vip_at = ? WHERE discord_id = ?",
        (ts, discord_id),
    )
    conn.execute(
        """
        INSERT INTO assignment_log (discord_id, role, timestamp, assigned_by, is_backfill)
        VALUES (?, 'vip', ?, ?, 0)
        """,
        (discord_id, ts, assigned_by),
    )
    conn.commit()
    conn.close()
    return ts


# ---------- Backfill / manual history correction ----------

def log_conductor(discord_id: str, name: str, date_iso: str, logged_by: str):
    ensure_member(discord_id, name)
    conn = get_connection()
    conn.execute(
        "UPDATE members SET last_conducted_at = ? WHERE discord_id = ? "
        "AND (last_conducted_at IS NULL OR last_conducted_at < ?)",
        (date_iso, discord_id, date_iso),
    )
    conn.execute(
        """
        INSERT INTO assignment_log (discord_id, role, timestamp, assigned_by, is_backfill)
        VALUES (?, 'conductor', ?, ?, 1)
        """,
        (discord_id, date_iso, logged_by),
    )
    conn.commit()
    conn.close()


def log_vip(discord_id: str, name: str, date_iso: str, logged_by: str):
    ensure_member(discord_id, name)
    conn = get_connection()
    conn.execute(
        "UPDATE members SET last_vip_at = ? WHERE discord_id = ? "
        "AND (last_vip_at IS NULL OR last_vip_at < ?)",
        (date_iso, discord_id, date_iso),
    )
    conn.execute(
        """
        INSERT INTO assignment_log (discord_id, role, timestamp, assigned_by, is_backfill)
        VALUES (?, 'vip', ?, ?, 1)
        """,
        (discord_id, date_iso, logged_by),
    )
    conn.commit()
    conn.close()


# ---------- History views ----------

def get_history_for(discord_id: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM members WHERE discord_id = ?", (discord_id,)
    ).fetchone()
    logs = conn.execute(
        """
        SELECT role, timestamp, is_backfill FROM assignment_log
        WHERE discord_id = ? ORDER BY timestamp DESC LIMIT 10
        """,
        (discord_id,),
    ).fetchall()
    conn.close()
    return row, logs


def get_full_roster_by_fairness():
    """Members sorted so whoever hasn't conducted in longest comes first.
    Members who have never conducted come first of all."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT discord_id, name, last_conducted_at, last_vip_at
        FROM members
        ORDER BY last_conducted_at IS NOT NULL, last_conducted_at ASC
        """
    ).fetchall()
    conn.close()
    return rows
