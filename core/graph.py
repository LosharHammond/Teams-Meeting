"""
Microsoft Graph — stateless auth + HTTP helpers.

Stateless by design: every Vercel function invocation is a new process,
so we acquire a fresh token each time (< 1 s overhead, no security risk).
"""
import json
import time
import logging
from typing import Dict, List, Optional

import requests
from msal import ConfidentialClientApplication

from core.config import (
    TENANT_ID, CLIENT_ID, CLIENT_SECRET,
    ORGANIZER_EMAIL, GRAPH_V1,
)

log = logging.getLogger("summarizer.graph")

_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_RETRIES = 5
_BASE_BACKOFF = 2.0

# Module-level token cache (lives for the duration of one function invocation)
_token_cache: Dict = {"token": None, "expires_at": 0.0}
_organizer_oid: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_token() -> str:
    now = time.monotonic()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    app = ConfidentialClientApplication(
        client_id=CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError(
            f"Auth failed: {result.get('error_description', json.dumps(result))}"
        )
    _token_cache["token"] = result["access_token"]
    _token_cache["expires_at"] = now + int(result.get("expires_in", 3500))
    return _token_cache["token"]


def get_organizer_oid() -> str:
    global _organizer_oid
    if _organizer_oid:
        return _organizer_oid
    data = graph_get(f"{GRAPH_V1}/users/{ORGANIZER_EMAIL}")
    oid = data.get("id", "")
    if not oid:
        raise RuntimeError(f"Could not resolve OID for {ORGANIZER_EMAIL}")
    _organizer_oid = oid
    log.info("Organiser OID: %s", oid)
    return oid


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _request(method: str, url: str, extra_headers: Optional[Dict] = None, **kwargs) -> requests.Response:
    token_refreshed = False
    for attempt in range(_MAX_RETRIES):
        headers = {"Authorization": f"Bearer {get_token()}"}
        if extra_headers:
            headers.update(extra_headers)

        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)

        if resp.status_code == 401 and not token_refreshed:
            _token_cache["expires_at"] = 0
            token_refreshed = True
            continue

        if resp.status_code in _RETRYABLE:
            wait = max(
                int(resp.headers.get("Retry-After", 0)),
                _BASE_BACKOFF * (2 ** attempt),
            )
            log.warning("Graph %s — waiting %.1fs (attempt %d)", resp.status_code, wait, attempt + 1)
            time.sleep(min(wait, 30))   # cap at 30s inside serverless
            continue

        return resp

    raise RuntimeError(f"Graph call to {url} failed after {_MAX_RETRIES} attempts")


def graph_get(url: str, params: Optional[Dict] = None, silent_404: bool = False) -> Dict:
    all_values: List = []
    current_url: Optional[str] = url
    current_params = params

    while current_url:
        resp = _request("GET", current_url, params=current_params)
        if not resp.ok:
            if resp.status_code == 404 and silent_404:
                resp.raise_for_status()
            log.error("Graph GET %s %s — %.400s", resp.status_code, current_url, resp.text)
            resp.raise_for_status()

        data = resp.json()
        if "value" in data:
            all_values.extend(data["value"])
            current_url = data.get("@odata.nextLink")
            current_params = None
        else:
            return data

    return {"value": all_values}


def graph_post(url: str, body: Dict) -> Dict:
    resp = _request("POST", url, extra_headers={"Content-Type": "application/json"}, json=body)
    if not resp.ok:
        log.error("Graph POST %s %s — %.400s", resp.status_code, url, resp.text)
        resp.raise_for_status()
    return resp.json() if resp.content else {}


def graph_get_raw(url: str, accept: str = "application/octet-stream") -> bytes:
    resp = _request("GET", url, extra_headers={"Accept": accept})
    if not resp.ok:
        log.error("Graph raw GET %s %s", resp.status_code, url)
        resp.raise_for_status()
    return resp.content
