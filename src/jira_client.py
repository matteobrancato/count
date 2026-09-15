"""Read-only Jira Cloud client — enriches TestRail bug keys with live Jira data.

Used wherever the dashboard surfaces a JIRA key extracted from TestRail defect
fields (Runs tab bug tables, the case deep-dive, Dexter's bug tool): it adds
status, resolution, priority and fix versions, turning a bare "EE20-1234" into
an actionable row.

Strictly best-effort and read-only:
  * `available()` is False when the Atlassian secrets are missing — every
    caller must degrade gracefully (no Jira columns, no errors).
  * Issues are fetched individually (parallel + per-key cache) so one deleted
    or permission-blocked key can never break a whole batch.

Secrets (Streamlit Cloud):
    JIRA_URL           e.g. "https://elab-aswatson.atlassian.net/jira"
                       (a trailing "/jira" or "/" is normalised away)
    ATLASSIAN_USER     the account e-mail
    ATLASSIAN_API_KEY  API token from id.atlassian.com → Security → API tokens
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import streamlit as st
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_TIMEOUT = 10
_FIELDS  = "status,resolution,priority,fixVersions"

# Jira status categories → compact glyphs for tables/chips.
STATUS_GLYPH = {"done": "✅", "indeterminate": "🔷", "new": "⚪"}


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


@st.cache_data(ttl=600, show_spinner=False)
def fetch_issue(key: str) -> dict | None:
    """One issue, normalised — or None (missing key / no permission / no config).

    Per-key caching means a batch never refetches keys another view already
    resolved, and one bad key can't poison its neighbours."""
    conf = _conf()
    if not conf:
        return None
    base, user, token = conf
    try:
        resp = requests.get(
            f"{base}/rest/api/3/issue/{key}",
            params={"fields": _FIELDS},
            auth=HTTPBasicAuth(user, token),
            timeout=_TIMEOUT,
        )
        if not resp.ok:            # 404 deleted key, 403 permission — skip quietly
            return None
        f = resp.json().get("fields") or {}
        status   = (f.get("status") or {})
        category = ((status.get("statusCategory") or {}).get("key") or "").lower()
        versions = [v.get("name", "") for v in (f.get("fixVersions") or [])]
        return {
            "key":             key,
            "status":          status.get("name") or "—",
            "status_category": category,                     # new/indeterminate/done
            "glyph":           STATUS_GLYPH.get(category, "•"),
            "resolution":      ((f.get("resolution") or {}).get("name")) or None,
            "priority":        ((f.get("priority") or {}).get("name")) or None,
            "fix_versions":    [v for v in versions if v],
        }
    except Exception:                                                   # noqa: BLE001
        logger.exception("Jira fetch failed for %s", key)
        return None


def fetch_issues(keys: tuple[str, ...]) -> dict[str, dict]:
    """{key: normalised issue} for every key that resolves (parallel, cached)."""
    if not keys or not available():
        return {}
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(keys), 8)) as pool:
        futs = {pool.submit(fetch_issue, k): k for k in keys}
        for fut in as_completed(futs):
            try:
                info = fut.result()
            except Exception:                                           # noqa: BLE001
                continue
            if info:
                out[info["key"]] = info
    return out


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_projects() -> list[dict]:
    """All accessible Jira projects — [{key, name}], sorted by key.

    Paginated through `project/search`; empty list on any failure."""
    conf = _conf()
    if not conf:
        return []
    base, user, token = conf
    auth = HTTPBasicAuth(user, token)
    out: list[dict] = []
    start = 0
    try:
        while True:
            resp = requests.get(
                f"{base}/rest/api/3/project/search",
                params={"startAt": start, "maxResults": 50},
                auth=auth, timeout=_TIMEOUT,
            )
            if not resp.ok:
                break
            data = resp.json()
            for p in data.get("values", []):
                out.append({"key": p.get("key", ""), "name": p.get("name", "")})
            if data.get("isLast", True) or not data.get("values"):
                break
            start += len(data["values"])
        out.sort(key=lambda p: p["key"])
        return out
    except Exception:                                                   # noqa: BLE001
        logger.exception("Jira projects fetch failed")
        return out


@st.cache_data(ttl=600, show_spinner=False)
def fetch_versions(project_key: str) -> list[dict]:
    """Fix versions of a Jira project — unreleased first, most recent first.

    Each entry: {name, released, release_date}.  Empty list on any failure
    (unknown project, no permission, no config)."""
    conf = _conf()
    if not conf or not project_key.strip():
        return []
    base, user, token = conf
    try:
        resp = requests.get(
            f"{base}/rest/api/3/project/{project_key.strip()}/versions",
            auth=HTTPBasicAuth(user, token),
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            return []
        versions = [
            {
                "id":           str(v.get("id") or ""),
                "name":         v.get("name", ""),
                "released":     bool(v.get("released")),
                "release_date": v.get("releaseDate") or "",
            }
            for v in resp.json()
            if not v.get("archived")
        ]
        # Unreleased first; within each group, most recent release date first.
        versions.sort(key=lambda v: (v["released"], v["release_date"] or ""),
                      reverse=False)
        versions.sort(key=lambda v: v["release_date"] or "9999", reverse=True)
        versions.sort(key=lambda v: v["released"])
        return versions
    except Exception:                                                   # noqa: BLE001
        logger.exception("Jira versions fetch failed for %s", project_key)
        return []


@st.cache_data(ttl=300, show_spinner=False)
def count_issues(jql: str) -> int | None:
    """Issue count for a JQL query, or None when it can't be resolved.

    Tries the modern approximate-count endpoint first, then falls back to the
    legacy search's `total` — covering both API generations of Jira Cloud."""
    conf = _conf()
    if not conf:
        return None
    base, user, token = conf
    auth = HTTPBasicAuth(user, token)
    try:
        resp = requests.post(
            f"{base}/rest/api/3/search/approximate-count",
            json={"jql": jql}, auth=auth, timeout=_TIMEOUT,
        )
        if resp.ok:
            return int(resp.json().get("count", 0))
        resp = requests.get(
            f"{base}/rest/api/3/search",
            params={"jql": jql, "maxResults": 0}, auth=auth, timeout=_TIMEOUT,
        )
        if resp.ok:
            return int(resp.json().get("total", 0))
        logger.warning("Jira count failed (%s): %s", resp.status_code, jql)
        return None
    except Exception:                                                   # noqa: BLE001
        logger.exception("Jira count failed for %s", jql)
        return None


@st.cache_data(ttl=300, show_spinner=False)
def search_issues(jql: str, limit: int = 50) -> list[dict]:
    """Issues matching a JQL, normalised — [] when it can't be resolved.

    Read-only, like the rest of this client.  Bounded by *limit* so a broad
    query can never pull a whole project into the page.  Tries the current
    `search/jql` endpoint first, then the legacy `search` one, so it works on
    both Jira Cloud API generations (same approach as `count_issues`).
    """
    conf = _conf()
    if not conf or not jql.strip():
        return []
    base, user, token = conf
    auth   = HTTPBasicAuth(user, token)
    fields = "summary,status,priority,assignee,issuetype,updated"

    def _normalise(issues: list[dict]) -> list[dict]:
        out = []
        for it in issues:
            f        = it.get("fields") or {}
            status   = f.get("status") or {}
            category = ((status.get("statusCategory") or {}).get("key") or "").lower()
            out.append({
                "key":             it.get("key", ""),
                "summary":         (f.get("summary") or "").strip(),
                "status":          status.get("name") or "—",
                "status_category": category,
                "glyph":           STATUS_GLYPH.get(category, "•"),
                "priority":        ((f.get("priority") or {}).get("name")) or "—",
                "assignee":        ((f.get("assignee") or {}).get("displayName")) or "Unassigned",
                "type":            ((f.get("issuetype") or {}).get("name")) or "—",
                "updated":         (f.get("updated") or "")[:10],
            })
        return out

    try:
        resp = requests.post(
            f"{base}/rest/api/3/search/jql",
            json={"jql": jql, "maxResults": limit, "fields": fields.split(",")},
            auth=auth, timeout=_TIMEOUT,
        )
        if resp.ok:
            return _normalise(resp.json().get("issues") or [])
        resp = requests.get(
            f"{base}/rest/api/3/search",
            params={"jql": jql, "maxResults": limit, "fields": fields},
            auth=auth, timeout=_TIMEOUT,
        )
        if resp.ok:
            return _normalise(resp.json().get("issues") or [])
        logger.warning("Jira search failed (%s): %s", resp.status_code, jql)
        return []
    except Exception:                                                   # noqa: BLE001
        logger.exception("Jira search failed for %s", jql)
        return []


# ── user stories (AI Test Design) ─────────────────────────────────────────────
# Read-only like everything above: the story is fetched to be READ by the AI,
# never written back.

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

    Keeps what matters to someone reading acceptance criteria: paragraphs,
    headings, bullet and numbered lists (nested ones indented), tables as
    `a | b` rows, links and mentions by their visible text.
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


@st.cache_data(ttl=3600, show_spinner=False)
def _acceptance_field_ids() -> tuple[str, ...]:
    """Custom fields whose NAME says acceptance criteria.

    Every Jira site names and numbers this field its own way
    ("Acceptance Criteria", "Acceptance criteria (AC)", customfield_10034 …),
    so it is found by name rather than hardcoded; many teams use none at all
    and write the AC in the description, which is read anyway.
    """
    conf = _conf()
    if not conf:
        return ()
    base, user, token = conf
    try:
        resp = requests.get(f"{base}/rest/api/3/field",
                            auth=HTTPBasicAuth(user, token), timeout=_TIMEOUT)
        if not resp.ok:
            return ()
        return tuple(f["id"] for f in resp.json()
                     if "acceptance" in str(f.get("name", "")).lower())
    except Exception:
        logger.exception("Jira field list failed")
        return ()


@st.cache_data(ttl=600, show_spinner=False)
def fetch_story(key: str) -> dict:
    """One story's text for the AI: summary, description, acceptance criteria.

    Always returns a dict; `error` is set instead of raising, so one missing
    or forbidden key is reported next to the others rather than aborting them.
    """
    out = {"key": key, "summary": "", "type": "", "status": "",
           "description": "", "acceptance_criteria": "", "attachments": 0,
           "error": None}
    conf = _conf()
    if not conf:
        out["error"] = "Jira is not configured"
        return out
    base, user, token = conf
    ac_ids = _acceptance_field_ids()
    fields = ",".join(["summary", "description", "issuetype", "status",
                       "attachment", *ac_ids])
    try:
        resp = requests.get(f"{base}/rest/api/3/issue/{key}",
                            params={"fields": fields},
                            auth=HTTPBasicAuth(user, token), timeout=_TIMEOUT)
    except Exception as exc:                                            # noqa: BLE001
        out["error"] = f"could not reach Jira ({type(exc).__name__})"
        return out
    if resp.status_code in (401, 403):
        out["error"] = "no permission to read it"
        return out
    if resp.status_code == 404:
        out["error"] = "not found (or no permission)"
        return out
    if not resp.ok:
        out["error"] = f"Jira answered {resp.status_code}"
        return out

    f = resp.json().get("fields") or {}
    out["summary"]     = f.get("summary") or ""
    out["type"]        = (f.get("issuetype") or {}).get("name") or ""
    out["status"]      = (f.get("status") or {}).get("name") or ""
    out["description"] = adf_to_text(f.get("description")).strip()
    out["attachments"] = len(f.get("attachment") or [])
    ac = [adf_to_text(f.get(fid)).strip() for fid in ac_ids if f.get(fid)]
    out["acceptance_criteria"] = "\n\n".join(a for a in ac if a)
    return out
