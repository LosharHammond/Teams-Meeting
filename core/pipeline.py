"""
Full meeting processing pipeline.

Each step is isolated so one failure doesn't cascade.
"""
import hashlib
import html
import logging
import re
import textwrap
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests
from openai import OpenAI, APIError, APITimeoutError, RateLimitError

from core import db
from core.config import (
    GRAPH_V1, GRAPH_BETA, OPENAI_API_KEY, OPENAI_MODEL,
    SENDER_EMAIL, TRANSCRIPT_TIMEOUT_H,
)
from core.graph import get_organizer_oid, graph_get, graph_get_raw, graph_post
from core.scanner import get_ended_meetings
from core.template import render

log = logging.getLogger("summarizer.pipeline")

_EMAIL_RE   = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a professional meeting assistant. Given a meeting transcript,
    produce a clear, well-structured HTML meeting summary.

    Your output MUST be valid HTML (no markdown) using this exact structure:

    <h2>Meeting Summary: {meeting subject}</h2>
    <p><strong>Date:</strong> {date} | <strong>Duration:</strong> {approx duration}</p>

    <h3>Overview</h3>
    <p>2-3 sentence executive summary of what was discussed.</p>

    <h3>Key Discussion Points</h3>
    <ul>
      <li><strong>Topic:</strong> Brief description and conclusions.</li>
    </ul>

    <h3>Decisions Made</h3>
    <ul>
      <li>Each concrete decision, stated clearly.</li>
    </ul>

    <h3>Action Items</h3>
    <table border="1" cellpadding="8" cellspacing="0"
           style="border-collapse:collapse;width:100%;">
      <tr style="background-color:#f0f0f0;">
        <th>Action Item</th><th>Owner</th><th>Due Date</th>
      </tr>
    </table>

    <h3>Next Steps</h3>
    <ul><li>Follow-up meetings or pending items.</li></ul>

    Rules:
    - Extract action items with owners when speakers are identifiable.
    - If no decisions were made, say "No formal decisions were recorded."
    - Keep concise but comprehensive (300-500 words). Professional, neutral tone.
    - Output ONLY the HTML, no code fences or explanation.
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _valid_email(addr: str) -> bool:
    return bool(_EMAIL_RE.match(addr.strip()))


def _mask(emails: List[str]) -> List[str]:
    out = []
    for e in emails:
        p = e.split("@")
        out.append(f"{p[0][:1]}***@{p[1]}" if len(p) == 2 else "***")
    return out


def event_hash(event_id: str) -> str:
    return hashlib.sha256(event_id.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Online meeting resolution
# ---------------------------------------------------------------------------

def resolve_meeting(event: Dict) -> Optional[Dict]:
    """
    Map a calendar event to its /onlineMeetings resource via joinWebUrl filter.

    The ONLY valid lookup path is:
      GET /users/{oid}/onlineMeetings?$filter=joinWebUrl eq '...'

    This requires the Teams Application Access Policy to be active.
    /communications/onlineMeetings returns 400 for joinWebUrl (needs videoTeleconferenceId).
    Date-range listing (?startDateTime=...&endDateTime=...) is unsupported — returns 400.
    There is no fallback; joinWebUrl + policy is the only path.
    """
    join_url = ((event.get("onlineMeeting") or {}).get("joinUrl") or "").strip()

    if not join_url:
        log.warning("No joinUrl in calendar event for '%s' — cannot resolve", event.get("subject"))
        return None

    # Use the ACTUAL meeting organiser's OID — the policy (-Global) grants
    # app access to /onlineMeetings for any user in the tenant, but the
    # call must be scoped to the user who ORGANISED that specific meeting.
    org_email = (
        (event.get("organizer") or {})
        .get("emailAddress", {})
        .get("address") or ""
    ).strip().lower()
    oid = _resolve_user_oid(org_email)

    try:
        items = graph_get(
            f"{GRAPH_V1}/users/{oid}/onlineMeetings",
            {"$filter": f"joinWebUrl eq '{join_url}'"},
        ).get("value", [])
        if items:
            return items[0]
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", 0)
        if status == 403:
            log.warning(
                "403 — Teams Application Access Policy not yet active for OID %s.\n"
                "The policy must be GRANTED to this user. Run in Teams PowerShell (admin):\n"
                "  Grant-CsApplicationAccessPolicy "
                "-PolicyName MeetingSummarizerPolicy -Identity %s\n"
                "Or grant org-wide:\n"
                "  Grant-CsApplicationAccessPolicy "
                "-PolicyName MeetingSummarizerPolicy -Global\n"
                "Propagation takes up to 30 min. Service will keep retrying.",
                oid, oid,
            )
        else:
            log.warning("joinWebUrl lookup failed: HTTP %s", status)

    return None


def _resolve_user_oid(email: str) -> str:
    """
    Resolve any user's UPN/email to their Azure AD Object ID.

    Used so that meeting lookups always use the OID of the ACTUAL meeting
    organiser, not the fixed ORGANIZER_EMAIL account.  Any user in the org
    can organise a meeting; the Teams Application Access Policy (granted
    -Global) authorises the app to call /users/{any_oid}/onlineMeetings.

    Falls back to the configured organiser OID if the lookup fails (e.g.
    external organiser, guest account, or transient Graph error).
    """
    if not email:
        return get_organizer_oid()
    try:
        user = graph_get(
            f"{GRAPH_V1}/users/{email}",
            {"$select": "id"},
        )
        oid = user.get("id", "")
        if oid:
            log.debug("Resolved OID for %s → %s", email, oid)
            return oid
    except requests.HTTPError as exc:
        log.debug(
            "OID lookup failed for %s (HTTP %s) — falling back to configured organiser OID",
            email, getattr(exc.response, "status_code", "?"),
        )
    return get_organizer_oid()


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def _clean_vtt(vtt: str) -> str:
    lines = []
    for line in vtt.splitlines():
        line = line.strip()
        if (not line or line.startswith("WEBVTT") or line.startswith("NOTE")
                or "-->" in line or re.fullmatch(r"\d+", line)):
            continue
        lines.append(line)
    return "\n".join(lines)


def _truncate(text: str, max_chars: int = 55_000) -> str:
    if len(text) <= max_chars:
        return text
    cut = text.rfind(".", 0, max_chars)
    return text[: (cut + 1) if cut != -1 else max_chars] + "\n\n[Transcript truncated]"


def get_transcript_text(meeting_id: str, organizer_oid: str) -> Optional[str]:
    oid = organizer_oid
    try:
        transcripts = graph_get(
            f"{GRAPH_BETA}/users/{oid}/onlineMeetings/{meeting_id}/transcripts"
        ).get("value", [])
    except requests.HTTPError as exc:
        log.warning("Transcript list failed: HTTP %s", getattr(exc.response, "status_code", "?"))
        return None

    if not transcripts:
        return None

    parts = []
    for t in transcripts:
        try:
            raw = graph_get_raw(
                f"{GRAPH_BETA}/users/{oid}/onlineMeetings/{meeting_id}"
                f"/transcripts/{t['id']}/content",
                accept="text/vtt",
            )
            cleaned = _clean_vtt(raw.decode("utf-8", errors="replace"))
            if cleaned.strip():
                parts.append(cleaned)
        except (requests.HTTPError, UnicodeDecodeError) as exc:
            log.warning("Transcript segment %s failed: %s", t.get("id"), exc)

    return "\n\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def summarize(text: str, subject: str, date: str) -> str:
    client   = OpenAI(api_key=OPENAI_API_KEY)
    user_msg = (
        f"Meeting subject: {html.escape(subject)}\n"
        f"Meeting date: {date}\n\n"
        f"Transcript:\n{_truncate(text)}"
    )
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=2000,
            )
            content = resp.choices[0].message.content or ""
            if re.search(r"<[a-zA-Z][^>]*>", content):
                return content
            log.warning("OpenAI returned non-HTML (attempt %d)", attempt + 1)
            time.sleep(3 * (2 ** attempt))
        except (RateLimitError, APITimeoutError) as exc:
            wait = 3 * (2 ** attempt)
            log.warning("OpenAI %s — waiting %ds", type(exc).__name__, wait)
            time.sleep(wait)
        except APIError as exc:
            log.error("OpenAI API error: %s", exc)
            raise
    raise RuntimeError("OpenAI summarization failed after 4 attempts")


# ---------------------------------------------------------------------------
# Attendee resolution
# ---------------------------------------------------------------------------

def _resolve_name(name: str) -> Optional[str]:
    try:
        users = graph_get(
            f"{GRAPH_V1}/users",
            {"$filter": f"displayName eq '{name}'", "$select": "mail,userPrincipalName"},
        ).get("value", [])
        if users:
            addr = (users[0].get("mail") or users[0].get("userPrincipalName") or "").lower()
            return addr if _valid_email(addr) else None
    except requests.HTTPError:
        pass
    return None


def get_attendance_emails(meeting_id: str, organizer_oid: str) -> List[str]:
    oid    = organizer_oid
    emails: set = set()
    try:
        reports = graph_get(
            f"{GRAPH_V1}/users/{oid}/onlineMeetings/{meeting_id}/attendanceReports"
        ).get("value", [])
        for r in reports:
            records = graph_get(
                f"{GRAPH_V1}/users/{oid}/onlineMeetings/{meeting_id}"
                f"/attendanceReports/{r['id']}/attendanceRecords"
            ).get("value", [])
            for rec in records:
                addr = (rec.get("emailAddress") or "").strip().lower()
                if addr and _valid_email(addr):
                    emails.add(addr)
        log.info("Attendance report: %d joiners", len(emails))
    except requests.HTTPError as exc:
        status = getattr(exc.response, "status_code", 0)
        if status == 403:
            log.warning("Attendance report blocked (403) — Teams policy not active")
        else:
            log.warning("Attendance report unavailable (HTTP %s)", status)
    return sorted(emails)


def get_calendar_emails(event: Dict) -> List[str]:
    emails: set = set()
    for att in event.get("attendees", []):
        addr = (att.get("emailAddress", {}).get("address") or "").strip().lower()
        if addr and _valid_email(addr):
            emails.add(addr)
        else:
            name = (att.get("emailAddress", {}).get("name") or "").strip()
            if name:
                resolved = _resolve_name(name)
                if resolved:
                    emails.add(resolved)

    org = ((event.get("organizer") or {})
           .get("emailAddress", {}).get("address") or "").strip().lower()
    if org and _valid_email(org):
        emails.add(org)

    log.info("Calendar attendees: %d", len(emails))
    return sorted(emails)


def resolve_recipients(meeting_id: str, event: Dict, organizer_oid: str) -> List[str]:
    from_report   = set(get_attendance_emails(meeting_id, organizer_oid))
    from_calendar = set(get_calendar_emails(event))
    merged        = sorted(from_report | from_calendar)
    log.info(
        "Recipients: %d report + %d calendar = %d total | %s",
        len(from_report), len(from_calendar), len(merged), _mask(merged),
    )
    return merged


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(recipients: List[str], subject: str, summary_html: str) -> None:
    clean = sorted({r.strip().lower() for r in recipients if _valid_email(r.strip())})
    if not clean:
        raise ValueError("No valid recipients after validation")

    graph_post(
        f"{GRAPH_V1}/users/{SENDER_EMAIL}/sendMail",
        {
            "message": {
                "subject": f"Meeting Recap: {subject}",
                "body": {"contentType": "HTML", "content": render(summary_html)},
                "toRecipients": [{"emailAddress": {"address": r}} for r in clean],
            },
            "saveToSentItems": True,
        },
    )
    log.info("Email sent — '%s' to %d recipient(s)", subject, len(clean))


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_meeting(event: Dict) -> str:
    """
    Process one calendar event end-to-end.
    Returns the final state: 'DONE', 'TRANSCRIPT_PENDING', 'FAILED', or 'SKIPPED'.
    """
    subject      = event.get("subject") or "Untitled Meeting"
    ev_id        = event.get("id", "")
    ev_hash      = event_hash(ev_id)
    end_str      = (event.get("end") or {}).get("dateTime", "")
    start_str    = (event.get("start") or {}).get("dateTime", "")
    meeting_date = start_str[:10] if start_str else "Unknown"
    end_dt       = _parse_dt(end_str) if end_str else datetime.now(timezone.utc)
    pending_until = (end_dt + timedelta(hours=TRANSCRIPT_TIMEOUT_H)).isoformat()

    # ── Idempotency ────────────────────────────────────────────────────────
    row = db.get_row(ev_hash)
    if row:
        state = row["state"]
        if state == "DONE":
            log.debug("Already done: %s", subject)
            return "SKIPPED"
        if state == "FAILED":
            log.debug("Previously failed: %s (%s)", subject, row.get("fail_reason"))
            return "SKIPPED"
        if state == "TRANSCRIPT_PENDING" and db.is_expired(row):
            db.set_state(ev_hash, "FAILED", "Transcript deadline expired")
            log.warning("Abandoned (deadline expired): %s", subject)
            return "FAILED"

    # Resolve the meeting organiser's OID — done AFTER the idempotency check
    # so already-DONE/FAILED meetings skip the Graph call entirely.
    # All /users/{oid}/onlineMeetings calls MUST use the organiser's own OID;
    # any user in the org can organise a meeting, not just ORGANIZER_EMAIL.
    org_email = (
        (event.get("organizer") or {})
        .get("emailAddress", {})
        .get("address") or ""
    ).strip().lower()
    org_oid = _resolve_user_oid(org_email)

    # ── Resolve online meeting ─────────────────────────────────────────────
    meeting = resolve_meeting(event)
    if not meeting:
        db.upsert(
            event_hash=ev_hash, event_id=ev_id, meeting_id=None,
            subject=subject, meeting_end=end_str, pending_until=pending_until,
            state="TRANSCRIPT_PENDING",
        )
        log.info("Meeting not resolvable yet: '%s' — retrying", subject)
        return "TRANSCRIPT_PENDING"

    meeting_id = meeting["id"]
    db.upsert(
        event_hash=ev_hash, event_id=ev_id, meeting_id=meeting_id,
        subject=subject, meeting_end=end_str, pending_until=pending_until,
        state="NEW",
    )

    # ── Transcript ────────────────────────────────────────────────────────
    transcript = get_transcript_text(meeting_id, org_oid)
    if not transcript or len(transcript.strip()) < 50:
        db.set_state(ev_hash, "TRANSCRIPT_PENDING")
        log.info("No transcript yet for '%s'", subject)
        return "TRANSCRIPT_PENDING"

    log.info("Transcript ready for '%s' (%d chars)", subject, len(transcript))

    # ── Summarize ─────────────────────────────────────────────────────────
    summary_html = summarize(transcript, subject, meeting_date)

    # ── Recipients ────────────────────────────────────────────────────────
    recipients = resolve_recipients(meeting_id, event, org_oid)
    if not recipients:
        db.set_state(ev_hash, "FAILED", "No valid recipients")
        log.warning("No recipients for '%s'", subject)
        return "FAILED"

    # ── Send ──────────────────────────────────────────────────────────────
    send_email(recipients, subject, summary_html)
    db.set_state(ev_hash, "DONE")
    log.info("Pipeline complete for '%s'", subject)
    return "DONE"


def run_poll_cycle() -> Dict:
    """Run one full cycle. Returns a stats dict."""
    events = get_ended_meetings()
    stats  = {"scanned": len(events), "done": 0, "pending": 0, "failed": 0, "skipped": 0}

    for ev in events:
        subject = ev.get("subject", "Unknown")
        try:
            result = process_meeting(ev)
            key = "pending" if result == "TRANSCRIPT_PENDING" else result.lower()
            stats[key] = stats.get(key, 0) + 1
        except Exception:
            log.exception("Unhandled error processing '%s'", subject)
            stats["failed"] += 1

    return stats
