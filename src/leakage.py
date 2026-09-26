"""Leakage — production incidents from Jira, per Business Unit.

Source: every Jira issue of type "Production Incident", whatever its
ENVIRONMENT (decided with Matteo, 2026-09-26: UAT-only incidents are counted
too).  Read-only, through the existing Jira integration; nothing is parsed out
of e-mails.

The BU comes from the Jira PROJECT, because the "BU multi" field is empty on
every incident (checked on 911 of them).  Two projects serve several BUs and
cannot be split; they are shown as declared groups rather than divided by a
guess: SD20 → "Superdrug & Savers", EE20 → "Eastern Europe (EE20)".

Incidents are matched to TestRail cases through the cases' References field —
data already downloaded for the Backlog tab, so the match costs no TestRail
request.  That answers the question an automation team actually asks: did
this escape happen in an area an automated test covers?
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import streamlit as st

from . import jira_client
from .freshness import DAY_TTL

ISSUE_TYPE = "Production Incident"
# One year: enough for a 6-month period AND the 6 months before it.
FETCH_DAYS = 366
MAX_PAGES = 50                        # 5,000 issues; see jira_client.search_all
PERIODS: dict[str, int] = {"30 days": 30, "90 days": 90, "6 months": 182}
ENVIRONMENT_FIELD = "ENVIRONMENT"


@dataclass(frozen=True)
class Group:
    label: str                        # shown to the user
    projects: tuple[str, ...]         # Jira project keys
    bus: tuple[str, ...]              # dashboard BUs it stands for
    shared: bool = False              # one project, several BUs


GROUPS: tuple[Group, ...] = (
    Group("ICI Paris XL",       ("IPXL20",),   ("ICI Paris XL",)),
    Group("Kruidvat",           ("KRUIDVAT",), ("Kruidvat",)),
    Group("Marionnaud",         ("MRN20",),    ("Marionnaud",)),
    Group("The Perfume Shop",   ("TPS20",),    ("The Perfume Shop",)),
    Group("Superdrug & Savers", ("SD20",),
          ("Superdrug", "Savers", "Superdrug / Savers"), shared=True),
    Group("Eastern Europe (EE20)", ("EE20",),
          ("Drogas", "Watsons Turkey", "Watsons Ukraine"), shared=True),
)
OTHER = "Other Jira projects"


def group_for_bu(bu: str) -> Group | None:
    return next((g for g in GROUPS if bu in g.bus), None)


def group_for_project(project: str) -> str:
    return next((g.label for g in GROUPS if project in g.projects), OTHER)


# ── normalising ──────────────────────────────────────────────────────────────
NO_COMPONENT = "No component"
_APP = re.compile(r"\b(android|ios|app)\b", re.IGNORECASE)


def channel(components: list[str]) -> str:
    """Web or App, from the Jira components — "No component" when there are
    none (Kruidvat leaves them empty), never guessed."""
    if not components:
        return NO_COMPONENT
    return "App" if any(_APP.search(c) for c in components) else "Web"


def _value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return " + ".join(_value(x) for x in v if _value(x))
    if isinstance(v, dict):
        return str(v.get("value") or v.get("name") or "")
    return str(v)


def _created(text: str) -> datetime | None:
    try:
        return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def normalise(issue: dict, base_url: str, env_field: str | None) -> dict | None:
    f = issue.get("fields") or {}
    created = _created(f.get("created") or "")
    if created is None:
        return None
    components = [c.get("name", "") for c in (f.get("components") or []) if c.get("name")]
    project = (f.get("project") or {}).get("key", "")
    return {
        "key":         issue.get("key", ""),
        "url":         f"{base_url}/browse/{issue.get('key', '')}",
        "project":     project,
        "group":       group_for_project(project),
        "summary":     f.get("summary") or "",
        "created":     created,
        "month":       created.strftime("%Y-%m"),
        "priority":    _value(f.get("priority")) or "Not set",
        "status":      _value(f.get("status")),
        "environment": _value(f.get(env_field)) if env_field else "",
        "components":  components,
        "channel":     channel(components),
    }


@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def fetch() -> tuple[list[dict], bool]:
    """Every Production Incident of the last year, normalised; plus whether
    the page guard cut the list short (so the tab can say so)."""
    env_field = jira_client.field_ids_by_name((ENVIRONMENT_FIELD,)).get(ENVIRONMENT_FIELD)
    fields = ("project", "summary", "created", "priority", "status", "components",
              *((env_field,) if env_field else ()))
    raw = jira_client.search_all(
        f'issuetype = "{ISSUE_TYPE}" AND created >= -{FETCH_DAYS}d ORDER BY created DESC',
        fields, max_pages=MAX_PAGES)
    conf = jira_client._conf()
    base = conf[0] if conf else ""
    rows = [r for r in (normalise(i, base, env_field) for i in raw) if r]
    return rows, len(raw) >= MAX_PAGES * 100


# ── metrics ──────────────────────────────────────────────────────────────────
def window(rows: list[dict], days: int, now: datetime,
           offset: int = 0) -> list[dict]:
    """Incidents created in the `days` before `now - offset` days."""
    end = now - timedelta(days=offset)
    start = end - timedelta(days=days)
    return [r for r in rows if start < r["created"] <= end]


def change(current: int, previous: int) -> float | None:
    """Relative change vs the previous period; None when there is nothing to
    compare with (a +∞% would be arithmetic, not information)."""
    if previous == 0:
        return None
    return (current - previous) / previous * 100


# ── the link to TestRail ─────────────────────────────────────────────────────
def cases_by_key(refs_by_case: dict[int, str]) -> dict[str, set[int]]:
    """{Jira key: TestRail case ids citing it} from the cases' References."""
    out: dict[str, set[int]] = {}
    for case_id, refs in refs_by_case.items():
        for key in jira_client.extract_issue_keys(refs or ""):
            out.setdefault(key, set()).add(case_id)
    return out


COVERED_AUTOMATED = "Linked to an automated test"
COVERED_MANUAL    = "Linked to tests, none automated"
NOT_LINKED        = "No linked test"


def coverage(key: str, by_key: dict[str, set[int]], automated: set[int]) -> str:
    cases = by_key.get(key)
    if not cases:
        return NOT_LINKED
    return COVERED_AUTOMATED if cases & automated else COVERED_MANUAL
