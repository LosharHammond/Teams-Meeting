"""
Railway Cron Entry Point
========================
Called by Railway's Cron Service every minute so meeting recaps are
delivered within 2-5 minutes of a meeting ending — same behaviour as
Fireflies.ai but without a visible bot joining the call.

How it works:
  1. Every minute: scan all org calendars for meetings that ended in the
     last 4 hours (LOOKBACK_HOURS).
  2. For each ended meeting: fetch the Teams-native transcript via Graph API.
  3. If transcript is ready: summarise with OpenAI → email all attendees.
  4. Already-processed meetings are skipped instantly via the DB state machine.
  5. Teams transcription must be enabled in Teams Admin Centre:
       Meetings → Meeting policies → Recording & transcription
       → Allow transcription: ON

In Railway dashboard:
  New Service → Cron Job
  Command : python cron.py
  Schedule: * * * * *   (every minute — requires Railway Hobby $5/mo or above)
"""
import logging
import sys
import os

# Ensure project root is importable (same as api/index.py does for Vercel)
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("summarizer.cron")

if __name__ == "__main__":
    from core.pipeline import run_poll_cycle
    log.info("Cron job started")
    try:
        stats = run_poll_cycle()
        log.info("Cron job complete: %s", stats)
        sys.exit(0)
    except Exception as exc:
        log.exception("Cron job failed: %s", exc)
        sys.exit(1)
