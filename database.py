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
    # remind_at = the exact moment (ISO, server-local) the 30-minutes-before
    # reminder should fire, computed once when the conductor is picked. Using
    # an absolute timestamp (rather than matching HH:MM strings each minute)
    # is what lets this correctly handle someone scheduled in the first 30
    # minutes after midnight, where the naive "30 min before" clock time
    # wraps into the previous day.
    # reminder_sent_at = when the reminder actually fired. Reset to NULL
    # whenever the conductor changes (e.g. via skip) so a fresh reminder
    # goes out for the new pick.
    # conductor_prev_last_conducted_at / vip_prev_last_vip_at = whatever the
    # CURRENTLY credited pick's last_conducted_at / last_vip_at was right
    # before they were credited for today. If leadership then overrides that
    # pick with someone else via /assign-conductor or /assign-vip, this is
    # what gets restored to the person being replaced — since they never
    # actually went, they shouldn't lose their place in the fairness order.
    # (/skip-conductor and /skip-vip are different: they mean "this person
    # really did already go, just not through the bot," so they deliberately
    # do NOT restore the outgoing person's value.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_run (
            server_date TEXT PRIMARY KEY,
            conductor_discord_id TEXT,
            vip_discord_id TEXT,
            notified_at TEXT,
            reminder_sent_at TEXT,
            remind_at TEXT,
            conductor_prev_last_conducted_at TEXT,
            vip_prev_last_vip_at TEXT
        )
    """)
    existing_daily_run_cols = [row["name"] for row in cur.execute("PRAGMA table_info(daily_run)").fetchall()]
    if "reminder_sent_at" not in existing_daily_run_cols:
        cur.execute("ALTER TABLE daily_run ADD COLUMN reminder_sent_at TEXT")
    if "remind_at" not in existing_daily_run_cols:
        cur.execute("ALTER TABLE daily_run ADD COLUMN remind_at TEXT")
    if "conductor_prev_last_conducted_at" not in existing_daily_run_cols:
        cur.execute("ALTER TABLE daily_run ADD COLUMN conductor_prev_last_conducted_at TEXT")
    if "vip_prev_last_vip_at" not in existing_daily_run_cols:
        cur.execute("ALTER TABLE daily_run ADD COLUMN vip_prev_last_vip_at TEXT")

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

    # Tracks members who've been DM'd asking for their preferred conductor
    # time and haven't replied with a valid one yet. When they DM the bot
    # back, on_message checks this table to know whether to treat their
    # message as a time-onboarding answer.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending_onboarding (
            discord_id TEXT PRIMARY KEY,
            prompted_at TEXT NOT NULL
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


def set_conductor_time(discord_id: str, preferred_time: str) -> bool:
    """Leadership override: change an existing conductor-rotation member's
    preferred time without resetting their joined_at (and therefore without
    disturbing their tie-break position in the fairness order). Returns
    False if they're not currently in the rotation — this only edits an
    existing entry, it doesn't add someone new."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE conductor_queue SET preferred_time = ? WHERE discord_id = ?",
        (preferred_time, discord_id),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_conductor_queue():
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT cq.discord_id, m.name, cq.preferred_time, cq.joined_at, m.last_conducted_at
        FROM conductor_queue cq
        JOIN members m ON m.discord_id = cq.discord_id
        ORDER BY m.last_conducted_at IS NOT NULL, m.last_conducted_at ASC, cq.joined_at ASC
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
    conducted longest (never-conducted members first). Ties (e.g. multiple
    members who've never conducted) break by who joined the rotation
    earliest, so a brand-new signup can't leapfrog someone who's been
    waiting longer. Being picked does not remove anyone from the list."""
    conn = get_connection()
    where, params = _exclude_clause(exclude_ids, "cq")
    query = f"""
        SELECT cq.discord_id, m.name, cq.preferred_time, m.last_conducted_at
        FROM conductor_queue cq
        JOIN members m ON m.discord_id = cq.discord_id
        {where}
        ORDER BY m.last_conducted_at IS NOT NULL, m.last_conducted_at ASC, cq.joined_at ASC
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


def is_in_vip_queue(discord_id: str) -> bool:
    """Checks membership without touching joined_at, so a leadership add
    command can tell 'already in' from 'needs adding' without accidentally
    resetting an existing member's position via join_vip_queue's upsert."""
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM vip_queue WHERE discord_id = ?", (discord_id,)).fetchone()
    conn.close()
    return row is not None


def get_conductor_ids():
    """The set of discord_id strings currently in the conductor rotation."""
    conn = get_connection()
    rows = conn.execute("SELECT discord_id FROM conductor_queue").fetchall()
    conn.close()
    return {row["discord_id"] for row in rows}


def mark_pending_onboarding(discord_id: str):
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO pending_onboarding (discord_id, prompted_at) VALUES (?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET prompted_at = excluded.prompted_at
        """,
        (discord_id, now_iso()),
    )
    conn.commit()
    conn.close()


def is_pending_onboarding(discord_id: str) -> bool:
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM pending_onboarding WHERE discord_id = ?", (discord_id,)).fetchone()
    conn.close()
    return row is not None


def clear_pending_onboarding(discord_id: str):
    conn = get_connection()
    conn.execute("DELETE FROM pending_onboarding WHERE discord_id = ?", (discord_id,))
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
        ORDER BY m.last_vip_at IS NOT NULL, m.last_vip_at ASC, vq.joined_at ASC
        """
    ).fetchall()
    conn.close()
    return rows


def get_next_vip(exclude_ids: Optional[Sequence[str]] = None):
    """Fairness pick from the persistent VIP list: whoever hasn't been VIP
    longest (never-VIP members first). Ties break by who joined the rotation
    earliest, same reasoning as get_next_conductor. Pass exclude_ids to skip
    specific members (e.g. today's conductor). Being picked does not remove
    anyone from the list."""
    conn = get_connection()
    where, params = _exclude_clause(exclude_ids, "vq")
    query = f"""
        SELECT vq.discord_id, m.name, m.last_vip_at
        FROM vip_queue vq
        JOIN members m ON m.discord_id = vq.discord_id
        {where}
        ORDER BY m.last_vip_at IS NOT NULL, m.last_vip_at ASC, vq.joined_at ASC
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
        INSERT INTO daily_run (server_date, conductor_discord_id, vip_discord_id, notified_at, reminder_sent_at,
                                conductor_prev_last_conducted_at, vip_prev_last_vip_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(server_date) DO UPDATE SET
            conductor_discord_id = excluded.conductor_discord_id,
            vip_discord_id = excluded.vip_discord_id,
            notified_at = excluded.notified_at,
            conductor_prev_last_conducted_at = excluded.conductor_prev_last_conducted_at,
            vip_prev_last_vip_at = excluded.vip_prev_last_vip_at
        """,
        (
            server_date,
            conductor["discord_id"] if conductor else None,
            vip["discord_id"] if vip else None,
            ts,
            conductor["last_conducted_at"] if conductor else None,
            vip["last_vip_at"] if vip else None,
        ),
    )
    conn.commit()
    conn.close()
    return conductor, vip


def override_conductor(server_date: str, discord_id: str, name: str, assigned_by: str):
    """Leadership manually /assign-conductor's someone, replacing whoever (if
    anyone) is currently credited for today. If that's a different person,
    they never actually conducted, so their fairness credit is reverted back
    to what it was before today's pick — they don't lose their place in line
    for a turn they didn't get. Locks in the new pick, remembers ITS
    pre-credit value too (so a later override can revert fairly again), and
    marks the reminder as already 'sent' since the assignment just happened live."""
    ensure_member(discord_id, name)
    ts = now_iso()
    conn = get_connection()

    run = conn.execute("SELECT * FROM daily_run WHERE server_date = ?", (server_date,)).fetchone()
    if run and run["conductor_discord_id"] and run["conductor_discord_id"] != discord_id:
        conn.execute(
            "UPDATE members SET last_conducted_at = ? WHERE discord_id = ?",
            (run["conductor_prev_last_conducted_at"], run["conductor_discord_id"]),
        )

    prev_row = conn.execute("SELECT last_conducted_at FROM members WHERE discord_id = ?", (discord_id,)).fetchone()
    prev_last_conducted_at = prev_row["last_conducted_at"] if prev_row else None

    conn.execute("UPDATE members SET last_conducted_at = ? WHERE discord_id = ?", (ts, discord_id))
    conn.execute(
        """
        INSERT INTO daily_run (server_date, conductor_discord_id, vip_discord_id, notified_at, reminder_sent_at,
                                conductor_prev_last_conducted_at)
        VALUES (?, ?, NULL, ?, ?, ?)
        ON CONFLICT(server_date) DO UPDATE SET
            conductor_discord_id = excluded.conductor_discord_id,
            notified_at = excluded.notified_at,
            reminder_sent_at = excluded.reminder_sent_at,
            conductor_prev_last_conducted_at = excluded.conductor_prev_last_conducted_at
        """,
        (server_date, discord_id, ts, ts, prev_last_conducted_at),
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


def override_vip(server_date: str, discord_id: str, name: str, assigned_by: str):
    """Same idea as override_conductor, for the VIP pick."""
    ensure_member(discord_id, name)
    ts = now_iso()
    conn = get_connection()

    run = conn.execute("SELECT * FROM daily_run WHERE server_date = ?", (server_date,)).fetchone()
    if run and run["vip_discord_id"] and run["vip_discord_id"] != discord_id:
        conn.execute(
            "UPDATE members SET last_vip_at = ? WHERE discord_id = ?",
            (run["vip_prev_last_vip_at"], run["vip_discord_id"]),
        )

    prev_row = conn.execute("SELECT last_vip_at FROM members WHERE discord_id = ?", (discord_id,)).fetchone()
    prev_last_vip_at = prev_row["last_vip_at"] if prev_row else None

    conn.execute("UPDATE members SET last_vip_at = ? WHERE discord_id = ?", (ts, discord_id))
    conn.execute(
        """
        INSERT INTO daily_run (server_date, conductor_discord_id, vip_discord_id, notified_at, vip_prev_last_vip_at)
        VALUES (?, NULL, ?, ?, ?)
        ON CONFLICT(server_date) DO UPDATE SET
            vip_discord_id = excluded.vip_discord_id,
            vip_prev_last_vip_at = excluded.vip_prev_last_vip_at
        """,
        (server_date, discord_id, ts, prev_last_vip_at),
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


def undo_conductor(server_date: str):
    """Corrects a case where whoever the bot credited as today's conductor
    never actually went (e.g. someone outside the rotation ran it in-game
    without leadership ever running /assign-conductor, so the bot had no way
    to know). Reverts that person's fairness credit back to what it was
    before today's pick and reopens today's conductor slot, WITHOUT picking
    a replacement — unlike /skip-conductor, which assumes a real turn
    happened and moves on. Returns the name of the person who got reverted,
    or None if there was no conductor credited today to undo."""
    run = get_todays_run(server_date)
    if run is None or run["conductor_discord_id"] is None:
        return None

    conn = get_connection()
    old_row = conn.execute(
        "SELECT name FROM members WHERE discord_id = ?", (run["conductor_discord_id"],)
    ).fetchone()
    old_name = old_row["name"] if old_row else "someone"

    conn.execute(
        "UPDATE members SET last_conducted_at = ? WHERE discord_id = ?",
        (run["conductor_prev_last_conducted_at"], run["conductor_discord_id"]),
    )
    conn.execute(
        "UPDATE daily_run SET conductor_discord_id = NULL, remind_at = NULL, "
        "reminder_sent_at = NULL, conductor_prev_last_conducted_at = NULL WHERE server_date = ?",
        (server_date,),
    )
    conn.commit()
    conn.close()
    return old_name


def undo_vip(server_date: str):
    """Same idea as undo_conductor, for the VIP pick."""
    run = get_todays_run(server_date)
    if run is None or run["vip_discord_id"] is None:
        return None

    conn = get_connection()
    old_row = conn.execute(
        "SELECT name FROM members WHERE discord_id = ?", (run["vip_discord_id"],)
    ).fetchone()
    old_name = old_row["name"] if old_row else "someone"

    conn.execute(
        "UPDATE members SET last_vip_at = ? WHERE discord_id = ?",
        (run["vip_prev_last_vip_at"], run["vip_discord_id"]),
    )
    conn.execute(
        "UPDATE daily_run SET vip_discord_id = NULL, vip_prev_last_vip_at = NULL WHERE server_date = ?",
        (server_date,),
    )
    conn.commit()
    conn.close()
    return old_name


def mark_reminder_sent(server_date: str):
    conn = get_connection()
    conn.execute(
        "UPDATE daily_run SET reminder_sent_at = ? WHERE server_date = ?",
        (now_iso(), server_date),
    )
    conn.commit()
    conn.close()


def set_remind_at(server_date: str, remind_at_iso: str):
    """Stores the exact moment (ISO, server-local) the 30-minutes-before
    reminder should fire for today's run."""
    conn = get_connection()
    conn.execute(
        "UPDATE daily_run SET remind_at = ? WHERE server_date = ?",
        (remind_at_iso, server_date),
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
        "UPDATE daily_run SET conductor_discord_id = ?, reminder_sent_at = NULL, "
        "conductor_prev_last_conducted_at = ? WHERE server_date = ?",
        (new_conductor["discord_id"], new_conductor["last_conducted_at"], server_date),
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
        "UPDATE daily_run SET vip_discord_id = ?, vip_prev_last_vip_at = ? WHERE server_date = ?",
        (new_vip["discord_id"], new_vip["last_vip_at"], server_date),
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
