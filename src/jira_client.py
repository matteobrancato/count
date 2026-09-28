"""Read-only Jira Cloud client.

One consumer today, on demand: the Leakage tab — every "Production
Incident", paginated to the end (`search_all`), with custom field ids resolved
by name, and the issue keys TestRail cases cite (`extract_issue_keys`).

Strictly best-effort and read-only: `available()` is False when the Atlassian
secrets are missing, and every caller degrades without an error.

Secrets (Streamlit Cloud):
    JIRA_URL           e.g. "https://elab-aswatson.atlassian.net/jira"
                       (a trailing "/jira" or "/" is normalised away)
    ATLASSIAN_USER     the account e-mail
    ATLASSIAN_API_KEY  API token from id.atlassian.com → Security → API tokens
"""
from __future__ import annotations

import logging
import re

import requests
import streamlit as st
from requests.auth import HTTPBasicAuth

from .freshness import DAY_TTL

logger = logging.getLogger(__name__)

_TIMEOUT = 10

def _conf() -> tuple[str, str, str] | None:
    """(base_url, user, token) from secrets, or None when not configured."""
    try:
        url   = str(st.secrets["JIRA_URL"]).rstrip("/")
        user  = str(st.secrets["ATLASSIAN_USER"])
        token = str(st.secrets["ATLASSIAN_API_KEY"])
    except Exception:                                                   # noqa: BLE001
        return None
    if not (url and user and token):
        return None
    # The browse/UI URL sometimes carries a "/jira" suffix — the REST API
    # lives at the bare site root.
    if url.endswith("/jira"):
        url = url[: -len("/jira")]
    return url, user, token


def available() -> bool:
    """True when the Atlassian secrets are configured."""
    return _conf() is not None


# ── issue keys ─────────────────────────────────────────────────────────────────
# How the Leakage tab matches an incident to the TestRail cases that cite it.

_ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")


def extract_issue_keys(text: str) -> list[str]:
    """Issue keys found in free text: bare keys and browse / board URLs alike.

    Case-insensitive on input ("ipxl20-15740" is still a key), de-duplicated,
    in the order they were written.
    """
    seen: dict[str, None] = {}
    for key in _ISSUE_KEY_RE.findall((text or "").upper()):
        seen.setdefault(key, None)
    return list(seen)


# ── production incidents (Leakage tab) ────────────────────────────────────────
@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def field_ids_by_name(names: tuple[str, ...]) -> dict[str, str]:
    """{field name: field id} for the named fields that exist on this site.

    Custom field ids differ from one Jira site to the next, so they are looked
    up by name rather than hardcoded.
    """
    conf = _conf()
    if not conf:
        return {}
    base, user, token = conf
    try:
        resp = requests.get(f"{base}/rest/api/3/field",
                            auth=HTTPBasicAuth(user, token), timeout=_TIMEOUT)
        if not resp.ok:
            return {}
        wanted = set(names)
        return {f["name"]: f["id"] for f in resp.json() if f.get("name") in wanted}
    except Exception:
        logger.exception("Jira field lookup failed")
        return {}


def search_all(jql: str, fields: tuple[str, ...], max_pages: int = 50) -> list[dict]:
    """EVERY issue matching a JQL (raw Jira JSON), paginated 100 at a time.

    Not capped at one page: the Leakage tab
    needs complete counts, and a silently truncated count is a wrong one.  The
    `max_pages` guard (5,000 issues) exists only to stop a mistyped JQL from
    walking a whole instance; hitting it is logged, never hidden.  Read-only.
    Raises on failure, so the caller can say the data is unavailable instead
    of showing zero incidents.
    """
    conf = _conf()
    if not conf:
        raise RuntimeError("Jira is not configured")
    base, user, token = conf
    auth = HTTPBasicAuth(user, token)
    issues: list[dict] = []
    token_next: str | None = None
    for _ in range(max_pages):
        body = {"jql": jql, "maxResults": 100, "fields": list(fields)}
        if token_next:
            body["nextPageToken"] = token_next
        resp = requests.post(f"{base}/rest/api/3/search/jql", json=body,
                             auth=auth, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"Jira search answered {resp.status_code}")
        payload = resp.json()
        issues.extend(payload.get("issues") or [])
        token_next = payload.get("nextPageToken")
        if not token_next or payload.get("isLast"):
            return issues
    logger.warning("Jira search stopped at %d pages (%d issues): %s",
                   max_pages, len(issues), jql)
    return issues
