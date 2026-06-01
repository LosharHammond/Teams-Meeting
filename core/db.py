"""
PostgreSQL state tracker.

State machine:
  NEW
    ├─► BOT_JOINING       (bot dispatched to join live meeting)
    │     └─► BOT_RECORDING   (bot confirmed in call, recording started)
    │           ├─► TRANSCRIPT_PENDING  (recording done, waiting for processing)
    │           └─► FAILED
    ├─► TRANSCRIPT_PENDING (waiting for Teams-native or bot transcript)
    └─► DONE / FAILED

Compatible with Neon, Supabase, Railway Postgres.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

from core.config import DATABASE_URL

log = logging.getLogger("summarizer.db")

# Base schema
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    event_hash          TEXT PRIMARY KEY,
    event_id            TEXT NOT NULL,
    meeting_id          TEXT,
    subject             TEXT,
    meeting_end         TEXT,
    pending_until       TEXT,
    state               TEXT NOT NULL DEFAULT 'NEW',
    fail_reason         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Bot recording columns — added with IF NOT EXISTS so existing DBs migrate safely
_MIGRATIONS = [
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS call_connection_id TEXT;",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS server_call_id     TEXT;",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS recording_id       TEXT;",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS recording_url      TEXT;",
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS join_url           TEXT;",
]


def _conn():
    """Open a short-lived connection. Caller must close it."""
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db() -> None:
    """Create / migrate schema. Called once at app startup."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
            for migration in _MIGRATIONS:
                cur.execute(migration)
        conn.commit()
    log.info("DB ready")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_row(event_hash: str) -> Optional[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM meetings WHERE event_hash = %s", (event_hash,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_row_by_call(call_connection_id: str) -> Optional[dict]:
    """Look up a meeting by ACS call_connection_id (used in webhook handler)."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM meetings WHERE call_connection_id = %s",
                (call_connection_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def all_rows() -> list:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_hash, subject, state, fail_reason, meeting_end, updated_at "
                "FROM meetings ORDER BY updated_at DESC LIMIT 200"
            )
            return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert(
    *,
    event_hash: str,
    event_id: str,
    meeting_id: Optional[str],
    subject: str,
    meeting_end: str,
    pending_until: str,
    state: str,
    fail_reason: Optional[str] = None,
    join_url: Optional[str] = None,
) -> None:
    """
    Insert or update — never downgrade a DONE/FAILED meeting back to NEW.
    """
    now = _now()
    sql = """
        INSERT INTO meetings
            (event_hash, event_id, meeting_id, subject, meeting_end,
             pending_until, state, fail_reason, join_url, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_hash) DO UPDATE SET
            meeting_id  = COALESCE(EXCLUDED.meeting_id,  meetings.meeting_id),
            join_url    = COALESCE(EXCLUDED.join_url,    meetings.join_url),
            state       = CASE
                            WHEN meetings.state IN ('DONE','FAILED') THEN meetings.state
                            ELSE EXCLUDED.state
                          END,
            fail_reason = EXCLUDED.fail_reason,
            updated_at  = EXCLUDED.updated_at
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (
                event_hash, event_id, meeting_id, subject, meeting_end,
                pending_until, state, fail_reason, join_url, now, now,
            ))
        conn.commit()


def set_state(event_hash: str, state: str, fail_reason: Optional[str] = None) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE meetings SET state=%s, fail_reason=%s, updated_at=%s "
                "WHERE event_hash=%s",
                (state, fail_reason, _now(), event_hash),
            )
        conn.commit()


def set_bot_joining(event_hash: str, call_connection_id: str) -> None:
    """Record that the ACS bot has been dispatched to join the meeting."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE meetings
                   SET state='BOT_JOINING',
                       call_connection_id=%s,
                       fail_reason=NULL,
                       updated_at=%s
                   WHERE event_hash=%s""",
                (call_connection_id, _now(), event_hash),
            )
        conn.commit()


def set_bot_recording(call_connection_id: str, server_call_id: str, recording_id: str) -> None:
    """Record that the bot is now in the call and recording has started."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE meetings
                   SET state='BOT_RECORDING',
                       server_call_id=%s,
                       recording_id=%s,
                       updated_at=%s
                   WHERE call_connection_id=%s""",
                (server_call_id, recording_id, _now(), call_connection_id),
            )
        conn.commit()


def set_recording_url(call_connection_id: str, recording_url: str) -> None:
    """Store the Azure Blob URL of the finished recording."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE meetings
                   SET recording_url=%s,
                       state='TRANSCRIPT_PENDING',
                       updated_at=%s
                   WHERE call_connection_id=%s""",
                (recording_url, _now(), call_connection_id),
            )
        conn.commit()


def reset_meetings(target: str = "failed") -> int:
    """Reset meetings to NEW. target: 'failed' | 'all' | 'done'"""
    where = {
        "failed": "state IN ('FAILED','BOT_JOINING','BOT_RECORDING')",
        "all":    "state != 'DONE'",
        "done":   "TRUE",
    }.get(target, "state IN ('FAILED','BOT_JOINING','BOT_RECORDING')")

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE meetings SET state='NEW', fail_reason=NULL, updated_at=%s WHERE {where}",
                (_now(),),
            )
            count = cur.rowcount
        conn.commit()
    return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_expired(row: dict) -> bool:
    pu = row.get("pending_until")
    if not pu:
        return False
    if isinstance(pu, str):
        deadline = datetime.fromisoformat(pu)
    else:
        deadline = pu
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > deadline
