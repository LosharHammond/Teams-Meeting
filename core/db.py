"""
PostgreSQL state tracker.

Replaces SQLite from the local version.  Compatible with:
  - Neon (free serverless Postgres)
  - Supabase (free)
  - Vercel Postgres (paid)

Set DATABASE_URL in your environment / Vercel dashboard.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

from core.config import DATABASE_URL

log = logging.getLogger("summarizer.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    event_hash      TEXT PRIMARY KEY,
    event_id        TEXT NOT NULL,
    meeting_id      TEXT,
    subject         TEXT,
    meeting_end     TEXT,
    pending_until   TEXT,
    state           TEXT NOT NULL DEFAULT 'NEW',
    fail_reason     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _conn():
    """Open a short-lived connection. Caller must close it."""
    # Neon / some providers use postgres:// — psycopg2 needs postgresql://
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db() -> None:
    """Create table if it doesn't exist. Called once at startup."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
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
) -> None:
    """
    Insert or update — never downgrade a DONE/FAILED meeting back to NEW.
    Uses PostgreSQL ON CONFLICT with a CASE expression.
    """
    now = _now()
    sql = """
        INSERT INTO meetings
            (event_hash, event_id, meeting_id, subject, meeting_end,
             pending_until, state, fail_reason, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_hash) DO UPDATE SET
            meeting_id  = COALESCE(EXCLUDED.meeting_id, meetings.meeting_id),
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
                pending_until, state, fail_reason, now, now,
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


def reset_meetings(target: str = "failed") -> int:
    """Reset meetings to NEW. target: 'failed' | 'all' | 'done'"""
    where = {
        "failed": "state = 'FAILED'",
        "all":    "state != 'DONE'",
        "done":   "TRUE",
    }.get(target, "state = 'FAILED'")

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
