"""Central configuration — reads from environment / .env file."""
import os
from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"Required env var {name!r} is not set.")
    return v


TENANT_ID             = _req("AZURE_TENANT_ID")
CLIENT_ID             = _req("AZURE_CLIENT_ID")
CLIENT_SECRET         = _req("AZURE_CLIENT_SECRET")
OPENAI_API_KEY        = _req("OPENAI_API_KEY")
ORGANIZER_EMAIL       = _req("ORGANIZER_EMAIL")
SENDER_EMAIL          = os.environ.get("SENDER_EMAIL", "").strip() or ORGANIZER_EMAIL
DATABASE_URL          = _req("DATABASE_URL")
CRON_SECRET           = os.environ.get("CRON_SECRET", "")

LOOKBACK_HOURS        = int(os.environ.get("LOOKBACK_HOURS", "24"))
TRANSCRIPT_TIMEOUT_H  = int(os.environ.get("TRANSCRIPT_TIMEOUT_HOURS", "26"))
OPENAI_MODEL          = os.environ.get("OPENAI_MODEL", "gpt-4o")

GRAPH_V1   = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"
