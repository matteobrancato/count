"""Read-only Jira Cloud client.

One consumer, on demand: the Leakage tab — the releases of a project
(`project_versions`), every Bug / Defect of a release and every Production
Incident after it, paginated to the end (`search_all`), custom field ids
resolved by name, rich text read as plain text (`adf_to_text`), and the issue
keys TestRail cases cite (`extract_issue_keys`).

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



def adf_to_text(node) -> str:
    """Atlassian Document Format (Jira Cloud's rich text) → readable plain text.

    Keeps what matters to someone reading an incident: paragraphs, headings,
    bullet and numbered lists (nested ones indented), tables as `a | b` rows,
    links and mentions by their visible text.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""

    kind    = node.get("type")
    attrs   = node.get("attrs") or {}
    content = node.get("content") or []

    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return attrs.get("text", "")
    if kind == "emoji":
        return attrs.get("text") or attrs.get("shortName", "")
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return attrs.get("url", "")
    if kind == "rule":
        return "\n"
    if kind in ("bulletList", "orderedList"):
        lines = []
        for i, item in enumerate(content, 1):
            body = adf_to_text(item.get("content") or []).strip("\n")
            marker = f"{i}. " if kind == "orderedList" else "- "
            first, *rest = body.split("\n") if body else [""]
            lines.append(marker + first)
            lines.extend("  " + r for r in rest)
        return "\n".join(lines) + "\n"
    if kind == "table":
        rows = []
        for row in content:
            cells = [adf_to_text(c.get("content") or []).strip().replace("\n", " ")
                     for c in (row.get("content") or [])]
            rows.append(" | ".join(cells))
        return "\n".join(rows) + "\n"
    inner = adf_to_text(content)
    if kind in ("paragraph", "heading", "codeBlock", "blockquote", "panel"):
        return inner.rstrip("\n") + "\n"
    return inner


# ── production incidents (Leakage tab) ────────────────────────────────────────
@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def project_versions(project: str) -> list[dict]:
    """Every fixVersion of a project (raw Jira JSON: name, released,
    releaseDate, startDate, archived …).  Raises on failure — the caller says
    the releases are unavailable rather than showing an empty list."""
    conf = _conf()
    if not conf:
        raise RuntimeError("Jira is not configured")
    base, user, token = conf
    resp = requests.get(f"{base}/rest/api/3/project/{project}/versions",
                        auth=HTTPBasicAuth(user, token), timeout=30)
    if not resp.ok:
        raise RuntimeError(f"Jira versions of {project} answered {resp.status_code}")
    return resp.json()


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


class QueryRejected(RuntimeError):
    """Jira refused the JQL itself (400) — e.g. a key that does not exist —
    as opposed to being unreachable."""


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
        if resp.status_code == 400:
            raise QueryRejected("Jira search answered 400")
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
