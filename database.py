"""
Simple SQLite data layer for the caravan bot.
Handles members, the conductor rotation, the VIP rotation, the daily run lock,
and the history log.

Both the conductor list and VIP list are PERSISTENT rotations, not one-time
queues: once someone signs up, they stay on the list forever (until removed),
and each server day the bot picks whoever's waited longest since their last
turn (never-had-a-turn members first). This is what makes it a fair round
robin across up to ~100 members without anyone needing to re-join.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

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
            FOREIGN KEY (discord_id) REFERENCES members (discord_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS vip_queue (
            discord_id TEXT PRIMARY KEY,
            joined_at TEXT NOT NULL,
            FOREIGN KEY (discord_id) REFERENCES members (discord_id)
        )
    """)

    # Tracks which conductor + VIP have been picked for a given game-server
    # calendar day. The in-game caravan can only run once every 24 hours, so
    # this is a single global lock keyed by day, not per queue entry.
    # notified_at = when today's picks were selected/announced (00:00 kickoff,
    # or a manual /assign-conductor / /assign-vip override).
    # reminder_sent_at = when the 30-minutes-before ping fired for today's
    # conductor. Reset to NULL whenever the conductor changes (e.g. via skip)
    # so a fresh reminder goes out for the new pick.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_run (
            server_date TEXT PRIMARY KEY,
            conductor_discord_id TEXT,
            vip_discord_id TEXT,
            notified_at TEXT,
            reminder_sent_at TEXT
        )
    """)
    existing_daily_run_cols = [row["name"] for row in cur.execute("PRAGMA table_info(daily_run)").fetchall()]
    if "reminder_sent_at" not in existing_daily_run_cols:
        cur.execute("ALTER TABLE daily_run ADD COLUMN reminder_sent_at TEXT")

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


def _exclude_clause(exclude_ids: Optional[Sequence[str]], table_alias: str):
    """Builds a 'WHERE alias.discord_id NOT IN (...)' clause and its params."""
    ids = [i for i in (exclude_ids or []) if i]
    if not ids:
        return "", []
    placeholders = ",".join("?" for _ in ids)
    return f" WHERE {table_alias}.discord_id NOT IN ({placeholders})", ids


# ---------- Conductor rotation (persistent list) ----------

def join_conductor_queue(discord_id: str, name: str, preferred_time: str):
    ensure_member(discord_id, name)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO conductor_queue (discord_id, preferred_time, joined_at)
        VALUES (?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            preferred_time = excluded.preferred_time,
            joined_at = excluded.joined_at
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
        ORDER BY m.last_conducted_at IS NOT NULL, m.last_conducted_at ASC
        """
    ).fetchall()
    conn.close()
    return rows


def get_conductor_entry(discord_id: str):
    """A single member's conductor-list row (name + preferred_time), if they're on it."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT cq.discord_id, m.name, cq.preferred_time
        FROM conductor_queue cq
        JOIN members m ON m.discord_id = cq.discord_id
        WHERE cq.discord_id = ?
        """,
        (discord_id,),
    ).fetchone()
    conn.close()
    return row


def get_next_conductor(exclude_ids: Optional[Sequence[str]] = None):
    """Fairness pick from the persistent conductor list: whoever hasn't
    conducted longest (never-conducted members first). Being picked does not
    remove anyone from the list."""
    conn = get_connection()
    where, params = _exclude_clause(exclude_ids, "cq")
    query = f"""
        SELECT cq.discord_id, m.name, cq.preferred_time, m.last_conducted_at
        FROM conductor_queue cq
        JOIN members m ON m.discord_id = cq.discord_id
        {where}
        ORDER BY m.last_conducted_at IS NOT NULL, m.last_conducted_at ASC
    """
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row


# ---------- VIP rotation (persistent list) ----------

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
        ORDER BY m.last_vip_at IS NOT NULL, m.last_vip_at ASC
        """
    ).fetchall()
    conn.close()
    return rows


def get_next_vip(exclude_ids: Optional[Sequence[str]] = None):
    """Fairness pick from the persistent VIP list: whoever hasn't been VIP
    longest (never-VIP members first). Pass exclude_ids to skip specific
    members (e.g. today's conductor). Being picked does not remove anyone
    from the list."""
    conn = get_connection()
    where, params = _exclude_clause(exclude_ids, "vq")
    query = f"""
        SELECT vq.discord_id, m.name, m.last_vip_at
        FROM vip_queue vq
        JOIN members m ON m.discord_id = vq.discord_id
        {where}
        ORDER BY m.last_vip_at IS NOT NULL, m.last_vip_at ASC
    """
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row


# ---------- Daily run lock (one conductor + one VIP per server day) ----------

def get_todays_run(server_date: str):
    """The daily_run row for this game-server date, or None if nothing's
    been picked yet for that day."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM daily_run WHERE server_date = ?", (server_date,)
    ).fetchone()
    conn.close()
    return row


def start_daily_run(server_date: str):
    """The 00:00 kickoff: picks today's conductor and VIP purely by fairness
    (not by anyone's preferred time), updates their last_conducted_at /
    last_vip_at, and locks the day. Returns (conductor_row, vip_row) — either
    can be None if that list is empty."""
    conductor = get_next_conductor()
    vip = get_next_vip(exclude_ids=[conductor["discord_id"]]) if conductor else None

    ts = now_iso()
    conn = get_connection()
    if conductor:
        conn.execute(
            "UPDATE members SET last_conducted_at = ? WHERE discord_id = ?",
            (ts, conductor["discord_id"]),
        )
        conn.execute(
            """
            INSERT INTO assignment_log (discord_id, role, timestamp, assigned_by, is_backfill)
            VALUES (?, 'conductor', ?, 'auto-rotation', 0)
            """,
            (conductor["discord_id"], ts),
        )
    if vip:
        conn.execute(
            "UPDATE members SET last_vip_at = ? WHERE discord_id = ?",
            (ts, vip["discord_id"]),
        )
        conn.execute(
            """
            INSERT INTO assignment_log (discord_id, role, timestamp, assigned_by, is_backfill)
            VALUES (?, 'vip', ?, 'auto-rotation', 0)
            """,
            (vip["discord_id"], ts),
        )
    conn.execute(
        """
        INSERT INTO daily_run (server_date, conductor_discord_id, vip_discord_id, notified_at, reminder_sent_at)
        VALUES (?, ?, ?, ?, NULL)
        ON CONFLICT(server_date) DO UPDATE SET
            conductor_discord_id = excluded.conductor_discord_id,
            vip_discord_id = excluded.vip_discord_id,
            notified_at = excluded.notified_at
        """,
        (server_date, conductor["discord_id"] if conductor else None, vip["discord_id"] if vip else None, ts),
    )
    conn.commit()
    conn.close()
    return conductor, vip


def record_manual_conductor(server_date: str, conductor_discord_id: str):
    """Leadership manually /assign-conductor'd someone out of band. Locks the
    day onto this pick without disturbing an existing VIP pick, and marks the
    30-min reminder as already 'sent' since the assignment already happened live."""
    ts = now_iso()
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO daily_run (server_date, conductor_discord_id, vip_discord_id, notified_at, reminder_sent_at)
        VALUES (?, ?, NULL, ?, ?)
        ON CONFLICT(server_date) DO UPDATE SET
            conductor_discord_id = excluded.conductor_discord_id,
            notified_at = excluded.notified_at,
            reminder_sent_at = excluded.reminder_sent_at
        """,
        (server_date, conductor_discord_id, ts, ts),
    )
    conn.commit()
    conn.close()


def record_manual_vip(server_date: str, vip_discord_id: str):
    """Leadership manually /assign-vip'd someone out of band. Locks in the VIP
    for today without disturbing an existing conductor pick."""
    ts = now_iso()
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO daily_run (server_date, conductor_discord_id, vip_discord_id, notified_at, reminder_sent_at)
        VALUES (?, NULL, ?, ?, NULL)
        ON CONFLICT(server_date) DO UPDATE SET
            vip_discord_id = excluded.vip_discord_id
        """,
        (server_date, vip_discord_id, ts),
    )
    conn.commit()
    conn.close()


def mark_reminder_sent(server_date: str):
    conn = get_connection()
    conn.execute(
        "UPDATE daily_run SET reminder_sent_at = ? WHERE server_date = ?",
        (now_iso(), server_date),
    )
    conn.commit()
    conn.close()


def skip_conductor(server_date: str):
    """Leadership determined today's conductor already had their turn without
    the bot knowing. Picks a replacement (excluding the old conductor and
    today's VIP), locks it in, and resets the reminder so the new pick gets
    their own. Returns (old_conductor_name, new_conductor_row); either half
    can be None if there was nothing to skip / no one left to pick."""
    run = get_todays_run(server_date)
    if run is None or run["conductor_discord_id"] is None:
        return None, None

    conn = get_connection()
    old_row = conn.execute(
        "SELECT name FROM members WHERE discord_id = ?", (run["conductor_discord_id"],)
    ).fetchone()
    conn.close()
    old_name = old_row["name"] if old_row else "someone"

    exclude = [run["conductor_discord_id"]]
    if run["vip_discord_id"]:
        exclude.append(run["vip_discord_id"])
    new_conductor = get_next_conductor(exclude_ids=exclude)
    if new_conductor is None:
        return old_name, None

    ts = now_iso()
    conn = get_connection()
    conn.execute(
        "UPDATE members SET last_conducted_at = ? WHERE discord_id = ?",
        (ts, new_conductor["discord_id"]),
    )
    conn.execute(
        "UPDATE daily_run SET conductor_discord_id = ?, reminder_sent_at = NULL WHERE server_date = ?",
        (new_conductor["discord_id"], server_date),
    )
    conn.execute(
        """
        INSERT INTO assignment_log (discord_id, role, timestamp, assigned_by, is_backfill)
        VALUES (?, 'conductor', ?, 'skip', 0)
        """,
        (new_conductor["discord_id"], ts),
    )
    conn.commit()
    conn.close()
    return old_name, new_conductor


def skip_vip(server_date: str):
    """Same idea as skip_conductor, for the VIP pick. Leaves the conductor
    pick untouched. Returns (old_vip_name, new_vip_row)."""
    run = get_todays_run(server_date)
    if run is None or run["vip_discord_id"] is None:
        return None, None

    conn = get_connection()
    old_row = conn.execute(
        "SELECT name FROM members WHERE discord_id = ?", (run["vip_discord_id"],)
    ).fetchone()
    conn.close()
    old_name = old_row["name"] if old_row else "someone"

    exclude = [run["vip_discord_id"]]
    if run["conductor_discord_id"]:
        exclude.append(run["conductor_discord_id"])
    new_vip = get_next_vip(exclude_ids=exclude)
    if new_vip is None:
        return old_name, None

    ts = now_iso()
    conn = get_connection()
    conn.execute(
        "UPDATE members SET last_vip_at = ? WHERE discord_id = ?",
        (ts, new_vip["discord_id"]),
    )
    conn.execute(
        "UPDATE daily_run SET vip_discord_id = ? WHERE server_date = ?",
        (new_vip["discord_id"], server_date),
    )
    conn.execute(
        """
        INSERT INTO assignment_log (discord_id, role, timestamp, assigned_by, is_backfill)
        VALUES (?, 'vip', ?, 'skip', 0)
        """,
        (new_vip["discord_id"], ts),
    )
    conn.commit()
    conn.close()
    return old_name, new_vip


def get_member_name(discord_id: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT name FROM members WHERE discord_id = ?", (discord_id,)
    ).fetchone()
    conn.close()
    return row


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
