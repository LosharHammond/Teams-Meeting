"""
Railway Cron Entry Point
========================
Called by Railway's cron service on schedule (0 14 * * * — 2 PM UTC daily).

In Railway dashboard:
  New Service → Cron Job
  Command : python cron.py
  Schedule: 0 14 * * *
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
