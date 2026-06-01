"""Central configuration — reads from environment / .env file."""
import os
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Required env var {name!r} is not set.")
    return v


# ── Required ──────────────────────────────────────────────────────────────────
TENANT_ID             = _req("AZURE_TENANT_ID")
CLIENT_ID             = _req("AZURE_CLIENT_ID")
CLIENT_SECRET         = _req("AZURE_CLIENT_SECRET")
OPENAI_API_KEY        = _req("OPENAI_API_KEY")
ORGANIZER_EMAIL       = _req("ORGANIZER_EMAIL")
SENDER_EMAIL          = os.environ.get("SENDER_EMAIL", "").strip() or ORGANIZER_EMAIL
DATABASE_URL          = _req("DATABASE_URL")
CRON_SECRET           = os.environ.get("CRON_SECRET", "")

# ── Timing ────────────────────────────────────────────────────────────────────
# How far back to scan for ended meetings on every poll cycle (default: 4h).
# Kept short because we poll every minute — no need to re-scan old history.
LOOKBACK_HOURS        = int(os.environ.get("LOOKBACK_HOURS", "4"))

# How long to wait for a transcript before giving up (default: 26h).
# 26h = 24h lookback + 2h buffer for very late transcripts.
TRANSCRIPT_TIMEOUT_H  = int(os.environ.get("TRANSCRIPT_TIMEOUT_HOURS", "26"))

# ── AI models ─────────────────────────────────────────────────────────────────
OPENAI_MODEL          = os.environ.get("OPENAI_MODEL", "gpt-4o")
WHISPER_MODEL         = os.environ.get("WHISPER_MODEL", "whisper-1")

# ── Graph endpoints ───────────────────────────────────────────────────────────
GRAPH_V1   = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"

# ── Bot / ACS recording (optional) ────────────────────────────────────────────
# When set the bot automatically joins every Teams meeting and records it,
# so recaps work even when participants don't start recording manually.
#
# Setup:
#   1. Create an Azure Communication Services resource in the Azure portal.
#   2. Copy the connection string → ACS_CONNECTION_STRING
#   3. Create an Azure Storage account → copy connection string →
#      AZURE_STORAGE_CONNECTION_STRING
#   4. Create a blob container named "recordings" in that storage account.
#   5. Set ACS_CALLBACK_BASE_URL to your Railway app URL
#      e.g. https://your-app.up.railway.app
#   6. Set ENABLE_BOT_RECORDING=true
#
# If ACS_CONNECTION_STRING is empty bot recording is silently disabled and
# the service falls back to Teams-native transcripts only.

ACS_CONNECTION_STRING      = os.environ.get("ACS_CONNECTION_STRING", "")
AZURE_STORAGE_CONN_STR     = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
ACS_CALLBACK_BASE_URL      = os.environ.get("ACS_CALLBACK_BASE_URL", "").rstrip("/")
BOT_DISPLAY_NAME           = os.environ.get("BOT_DISPLAY_NAME", "Procus Meeting Recorder")
RECORDINGS_CONTAINER       = os.environ.get("RECORDINGS_CONTAINER", "recordings")
ENABLE_BOT_RECORDING       = (
    os.environ.get("ENABLE_BOT_RECORDING", "false").lower() == "true"
    and bool(ACS_CONNECTION_STRING)
    and bool(AZURE_STORAGE_CONN_STR)
    and bool(ACS_CALLBACK_BASE_URL)
)
