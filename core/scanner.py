"""
Org-wide calendar scanner.

Uses a ThreadPoolExecutor to scan all licensed users' calendars in parallel.
Provides two scan modes:
  - get_ended_meetings()  — meetings that finished in the last LOOKBACK_HOURS
  - get_active_meetings() — meetings currently in progress (bot joining target)
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import requests

from core.config import GRAPH_V1, LOOKBACK_HOURS
from core.graph import get_organizer_oid, graph_get

log = logging.getLogger("summarizer.scanner")

_MAILBOX_SKIP_CODES = {
    "MailboxNotEnabledForRESTAPI",
    "ResourceNotFound",
    "ErrorItemNotFound",
}

_MAX_WORKERS = 12   # parallel calendar fetches — stays inside Graph rate limits


# ---------------------------------------------------------------------------
# User list
# ---------------------------------------------------------------------------

def get_licensed_user_ids() -> List[str]:
    try:
        users = graph_get(
            f"{GRAPH_V1}/users",
            {"$select": "id,assignedLicenses", "$top": "999"},
        ).get("value", [])
        ids = [u["id"] for u in users if u.get("assignedLicenses")]
        log.info("Org: %d licensed users", len(ids))
        return ids
    except requests.HTTPError as exc:
        log.warning("Could not list users (HTTP %s) — organiser only",
                    getattr(exc.response, "status_code", "?"))
        return [get_organizer_oid()]


# ---------------------------------------------------------------------------
# Calendar fetch for one user
# ---------------------------------------------------------------------------

def _fetch_user_calendar(oid: str, start: datetime, end: datetime) -> List[Dict]:
    try:
        return graph_get(
            f"{GRAPH_V1}/users/{oid}/calendarView",
            {
                "startDateTime": start.isoformat(),
                "endDateTime":   end.isoformat(),
                "$select": "id,subject,start,end,onlineMeeting,attendees,organizer,isOnlineMeeting",
                "$orderby": "start/dateTime desc",
                "$top": "50",
            },
            silent_404=True,
        ).get("value", [])
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", 0)
        if status == 404:
            try:
                code = exc.response.json().get("error", {}).get("code", "")
            except Exception:
                code = ""
            if code in _MAILBOX_SKIP_CODES or status == 404:
                return []
        log.warning("Calendar fetch failed for OID %s: HTTP %s", oid, status)
        return []


def _parse_dt(dt_str: str) -> datetime:
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Org-wide scan helpers
# ---------------------------------------------------------------------------

def _scan_all_calendars(start: datetime, end: datetime) -> Dict[str, Dict]:
    """
    Scan every licensed user's calendar for the given window.
    Returns a dict keyed by joinUrl → event (keeping the copy with most attendees).
    """
    user_ids = get_licensed_user_ids()
    log.info("Scanning %d calendars (%d workers)", len(user_ids), _MAX_WORKERS)

    seen: Dict[str, Dict] = {}

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_user_calendar, uid, start, end): uid
                   for uid in user_ids}
        for future in as_completed(futures):
            for ev in (future.result() or []):
                if not ev.get("isOnlineMeeting"):
                    continue
                key = (ev.get("onlineMeeting") or {}).get("joinUrl") or ev.get("id", "")
                if not key:
                    continue
                if key not in seen:
                    seen[key] = ev
                elif len(ev.get("attendees", [])) > len(seen[key].get("attendees", [])):
                    seen[key] = ev

    return seen


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_ended_meetings() -> List[Dict]:
    """
    Return deduplicated Teams meetings that ended within the last LOOKBACK_HOURS.
    These are candidates for transcript fetch + summarisation.
    """
    now   = datetime.now(timezone.utc)
    start = now - timedelta(hours=LOOKBACK_HOURS)

    seen  = _scan_all_calendars(start, now)

    ended = [
        ev for ev in seen.values()
        if (ev.get("end") or {}).get("dateTime", "")
        and _parse_dt((ev["end"])["dateTime"]) < now
    ]

    log.info("Ended meetings in last %dh: %d", LOOKBACK_HOURS, len(ended))
    return ended


def get_active_meetings() -> List[Dict]:
    """
    Return deduplicated Teams meetings that are CURRENTLY IN PROGRESS.
    These are candidates for bot joining — the bot must join before they end.

    A meeting is considered active if:
      - start.dateTime is in the past
      - end.dateTime is in the future (or up to 30 min in the past, to catch
        overruns — scheduled end times are often earlier than actual end)
    """
    now      = datetime.now(timezone.utc)
    # Look back 4h (meetings that started up to 4h ago and might still be running)
    # Look forward 15m (to catch meetings just about to start — bot joins early)
    scan_start = now - timedelta(hours=4)
    scan_end   = now + timedelta(minutes=15)

    seen = _scan_all_calendars(scan_start, scan_end)

    active = []
    for ev in seen.values():
        start_str = (ev.get("start") or {}).get("dateTime", "")
        end_str   = (ev.get("end")   or {}).get("dateTime", "")
        if not start_str or not end_str:
            continue
        meeting_start = _parse_dt(start_str)
        meeting_end   = _parse_dt(end_str)
        # Active = started ≤ now AND scheduled end > now - 30min
        if meeting_start <= now and meeting_end > now - timedelta(minutes=30):
            active.append(ev)

    log.info("Active meetings right now: %d", len(active))
    return active
