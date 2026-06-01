"""
Org-wide calendar scanner.

Uses a ThreadPoolExecutor to scan all licensed users' calendars in parallel
so the full scan stays well within Vercel's 60-second function timeout.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

from core.config import GRAPH_V1, LOOKBACK_HOURS, ORGANIZER_EMAIL
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

def _fetch_user_calendar(
    oid: str, start: datetime, end: datetime
) -> List[Dict]:
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
                return []   # expected — on-prem / inactive mailbox
        log.warning("Calendar fetch failed for OID %s: HTTP %s", oid, status)
        return []


# ---------------------------------------------------------------------------
# Org-wide scan
# ---------------------------------------------------------------------------

def _parse_dt(dt_str: str) -> datetime:
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_ended_meetings() -> List[Dict]:
    """
    Return deduplicated ended Teams meetings from every user's calendar.
    Parallel fetches via ThreadPoolExecutor.
    """
    now   = datetime.now(timezone.utc)
    start = now - timedelta(hours=LOOKBACK_HOURS)

    user_ids = get_licensed_user_ids()
    log.info("Scanning %d calendars (parallel, %d workers)", len(user_ids), _MAX_WORKERS)

    seen: Dict[str, Dict] = {}   # joinUrl → best event
    scanned = 0

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_user_calendar, uid, start, now): uid
                   for uid in user_ids}
        for future in as_completed(futures):
            events = future.result()
            if events is not None:
                scanned += 1
            for ev in (events or []):
                if not ev.get("isOnlineMeeting"):
                    continue
                end_str = (ev.get("end") or {}).get("dateTime", "")
                if not end_str or _parse_dt(end_str) >= now:
                    continue

                key = (ev.get("onlineMeeting") or {}).get("joinUrl") or ev.get("id", "")
                if not key:
                    continue

                if key not in seen:
                    seen[key] = ev
                elif len(ev.get("attendees", [])) > len(seen[key].get("attendees", [])):
                    seen[key] = ev   # keep copy with the most attendees

    ended = list(seen.values())
    log.info(
        "Scan done — %d mailboxes scanned, %d no-mailbox skipped, %d unique ended meetings",
        scanned, len(user_ids) - scanned, len(ended),
    )
    return ended
