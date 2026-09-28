"""Defect leakage per release — the figure Delivery reports every quarter.

A release is a Jira fixVersion of the BU's MAIN release stream.  Hotfixes are
separate versions ("EE_SAP_Hotfix1_2026Q2.Jun") and are not releases here:
Delivery's report skips them too, and a hotfix in between must not cut a
release's post-release window short.

    UAT issues   every Bug whose fixVersion is the release, any status (what
                 Delivery calls "UAT Bugs" + "Old Bugs"), plus every Defect of
                 the release created since the previous release shipped
    Leaked       every Production Incident created from the release date up
                 to the next release's date (or today, while there is none),
                 minus the exclusions below
    Leakage ratio         leaked ÷ UAT issues                  threshold 25%
    Leakage ratio (H/H)   leaked High/Highest ÷ UAT issues     threshold 10%

Both ratios divide by ALL UAT issues, exactly as Delivery's workbook does
(decided with Matteo on 2026-09-28; the e-mail's wording says "high-priority
UAT issues", the workbook's formula does not).

Checked on 2026-09-28 against Delivery's Q2 workbook and live Jira.  Bugs:
Delivery counts all of the fixVersion's, cancelled ones included.  Defects:
only those created from the release's UAT start date — a date from the
release calendar that Jira does not hold (KV_SAP_Release_2026Q2.1 has 18
Defects in Jira and 0 in the report).  The previous release's date stands in
for it, as Matteo's own description of the process has it: it reproduced
Delivery's Defect count on 6 of 8 Q2 releases (KV_SAP_Release_2026Q2.1 +1,
MRN_SAP_Release_2026Q2.1 −2, where UAT started before the previous release).
EE_SAP_Release_2026Q2.Apr: 77 Bugs + 12 Defects, 12 leaked, 6 High/Highest —
13.5%, identical to the report.  Incidents are edited after a report is sent
(EE20-42100's root cause changed on 2026-09-18), so a figure recounted today
can differ from one sent last quarter: the tab always shows today's Jira.

The exclusions are Delivery's JQL, word for word (Matteo, 2026-09-28):
    "Root Cause (EU)" not in (Requirement/Documentation, new requirement,
    non reproducible, Expected behaviour, data issue, duplicate, security
    issue, not applicable, Not a bug) AND status not in (cancelled)
    AND (component NOT IN ("Ios App", "Android App") OR component IS EMPTY)
    AND (labels NOT IN ("MAPP", "ios", "Android") OR labels IS EMPTY)
In JQL `not in` also drops an EMPTY root cause, so an incident nobody has
analysed yet is not counted either.  Kruidvat marks its app incidents with
"KV Team Ownership" = "MAPP Squad" rather than with app components or
labels, and Delivery leaves them out (0 of 1,338 rows in its KV sheet):
excluded here too (Matteo, 2026-09-28).  They are applied here in Python, not in
the JQL, so every excluded incident is still listed with the reason.

The BU comes from the Jira project.  EE20 and SD20 serve several BUs and
cannot be split, so they are shown as declared groups.  Trekpleister's
releases live in the KRUIDVAT project (TP_SAP_Release_*), but its incidents
cannot be told apart from Kruidvat's, so it has no group of its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import streamlit as st

from . import jira_client
from .freshness import DAY_TTL

# ── configuration ────────────────────────────────────────────────────────────
UAT_TYPES: tuple[str, ...] = ("Bug", "Defect")
INCIDENT_TYPE = "Production Incident"
LEAKAGE_THRESHOLD = 0.25
LEAKAGE_HH_THRESHOLD = 0.10
HIGH_PRIORITIES = frozenset({"Highest", "High"})

ROOT_CAUSE_FIELD = "Root Cause (EU)"
ENVIRONMENT_FIELD = "ENVIRONMENT"
TEXT_FIELDS: tuple[str, ...] = ("Steps to Reproduce", "Actual Results",
                                "Expected Results")
# Kruidvat's owning team; its app team's incidents are app incidents.
OWNERSHIP_FIELD = "KV Team Ownership"
# Where a reporter can name the TestRail case directly (94 of 3,828 incidents
# in the last year had one — the deterministic links, used before any guess).
CASE_FIELDS: tuple[str, ...] = ("TestRail Case ID", "Test Case Reference")

EXCLUDED_ROOT_CAUSES = frozenset(v.lower() for v in (
    "Requirement/Documentation", "new requirement", "non reproducible",
    "Expected behaviour", "data issue", "duplicate", "security issue",
    "not applicable", "Not a bug",
))
EXCLUDED_STATUSES = frozenset({"cancelled"})
EXCLUDED_COMPONENTS = frozenset({"ios app", "android app"})
EXCLUDED_LABELS = frozenset({"mapp", "ios", "android"})
EXCLUDED_OWNERS = frozenset({"mapp squad"})


@dataclass(frozen=True)
class Group:
    label: str                   # shown to the user
    project: str                 # Jira project key
    bus: tuple[str, ...]         # dashboard BUs it stands for
    release_pattern: str         # the main release stream's fixVersion names
    shared: bool = False         # one project, several BUs


GROUPS: tuple[Group, ...] = (
    Group("ICI Paris XL", "IPXL20", ("ICI Paris XL",),
          r"^IPXL_SAP_Release_\d{4}Q\d\.\d+$"),
    Group("Kruidvat", "KRUIDVAT", ("Kruidvat",),
          r"^KV_SAP_Release_\d{4}Q\d\.\d+$"),
    Group("Marionnaud", "MRN20", ("Marionnaud",),
          r"^MRN_SAP_Release_\d{4}Q\d\.\d+$"),
    Group("The Perfume Shop", "TPS20", ("The Perfume Shop",),
          r"^TPS_SAP_Release_\d{4}Q\d\.\d+$"),
    Group("Superdrug & Savers", "SD20", ("Superdrug", "Savers", "Superdrug / Savers"),
          r"^SD_Release_\d{4}Q\d\.\w+ SAP$", shared=True),
    Group("Eastern Europe (EE20)", "EE20", ("Drogas", "Watsons Turkey", "Watsons Ukraine"),
          r"^EE_SAP_Release_\d{4}Q\d\.\w+$", shared=True),
)


def group_for_bu(bu: str) -> Group | None:
    return next((g for g in GROUPS if bu in g.bus), None)


# ── releases ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Release:
    name: str
    released: bool
    date: date | None            # Jira's releaseDate (planned, while unreleased)


def _parse_date(text: str | None) -> date | None:
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def releases_from_versions(versions: list[dict], pattern: str) -> list[Release]:
    """The main-stream releases among a project's fixVersions, oldest first
    (undated ones last)."""
    rx = re.compile(pattern)
    out = [Release(v.get("name", ""), bool(v.get("released")),
                   _parse_date(v.get("releaseDate")))
           for v in versions if rx.match(v.get("name", ""))]
    return sorted(out, key=lambda r: (r.date is None, r.date or date.min, r.name))


def releases(group: Group) -> list[Release]:
    return releases_from_versions(jira_client.project_versions(group.project),
                                  group.release_pattern)


def latest_released(rels: list[Release]) -> Release | None:
    done = [r for r in rels if r.released and r.date]
    return done[-1] if done else None


@dataclass(frozen=True)
class Window:
    """The post-release window of one release."""
    release: Release
    previous: Release | None     # the release before it (context only)
    following: Release | None    # the next one, released or planned
    start: date                  # this release's date
    end: date                    # the next release's date, or today
    open_ended: bool             # True while the next release has not shipped


def previous_of(rels: list[Release], rel: Release) -> Release | None:
    """The last release shipped before `rel` (released or planned)."""
    if rel.date is None:
        return None
    return next((r for r in reversed(rels)
                 if r.released and r.date and r.date < rel.date), None)


def window_for(rels: list[Release], name: str, today: date) -> Window | None:
    """None for a release that has not shipped: it has no post-release window
    yet, only UAT issues."""
    rel = next((r for r in rels if r.name == name), None)
    if rel is None or not rel.released or rel.date is None:
        return None
    dated = [r for r in rels if r.date]
    previous = previous_of(rels, rel)
    following = next((r for r in dated if r.date > rel.date), None)
    shipped = following is not None and following.released and following.date <= today
    end = following.date if shipped else today
    return Window(rel, previous, following, rel.date, end, not shipped)


# ── Jira reads ───────────────────────────────────────────────────────────────
def _q(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(x for x in (_value(i) for i in v) if x)
    if isinstance(v, dict):
        if v.get("type") == "doc":
            return jira_client.adf_to_text(v).strip()
        return str(v.get("value") or v.get("name") or v.get("displayName") or "")
    return str(v)


def _created(text: str) -> datetime | None:
    try:
        return datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _base_row(issue: dict, base_url: str) -> dict:
    f = issue.get("fields") or {}
    key = issue.get("key", "")
    return {
        "key":        key,
        "url":        f"{base_url}/browse/{key}",
        "type":       _value(f.get("issuetype")),
        "summary":    f.get("summary") or "",
        "created":    _created(f.get("created") or ""),
        "priority":   _value(f.get("priority")) or "Not set",
        "status":     _value(f.get("status")),
        "components": [c.get("name", "") for c in (f.get("components") or []) if c.get("name")],
    }


@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def uat_issues(project: str, release: str) -> list[dict]:
    """Every Bug and Defect of the release's fixVersion (before `split_uat`)."""
    types = ", ".join(_q(t) for t in UAT_TYPES)
    raw = jira_client.search_all(
        f"project = {_q(project)} AND fixVersion = {_q(release)} "
        f"AND issuetype in ({types}) ORDER BY created ASC",
        ("summary", "issuetype", "created", "priority", "status", "components"))
    base = _base_url()
    return [_base_row(i, base) for i in raw]


def _field_ids() -> dict[str, str]:
    return jira_client.field_ids_by_name(
        (ROOT_CAUSE_FIELD, ENVIRONMENT_FIELD, OWNERSHIP_FIELD, *TEXT_FIELDS, *CASE_FIELDS))


def _base_url() -> str:
    conf = jira_client._conf()
    return conf[0] if conf else ""


def normalise_incident(issue: dict, base_url: str, ids: dict[str, str]) -> dict:
    """One Production Incident as the tab and the AI read it."""
    f = issue.get("fields") or {}
    row = _base_row(issue, base_url)

    def custom(name: str) -> str:
        fid = ids.get(name)
        return _value(f.get(fid)).strip() if fid else ""

    links = []
    for link in f.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue") or {}
        if other.get("key"):
            of = other.get("fields") or {}
            links.append({"key": other["key"],
                          "relation": (link.get("type") or {}).get(
                              "outward" if "outwardIssue" in link else "inward", ""),
                          "type": _value(of.get("issuetype")),
                          "summary": of.get("summary") or ""})
    row.update({
        "description": _value(f.get("description")).strip(),
        "labels":      list(f.get("labels") or []),
        "root_cause":  custom(ROOT_CAUSE_FIELD),
        "environment": custom(ENVIRONMENT_FIELD),
        "team_ownership": custom(OWNERSHIP_FIELD),
        "steps":       custom("Steps to Reproduce"),
        "actual":      custom("Actual Results"),
        "expected":    custom("Expected Results"),
        "case_refs":   " ".join(custom(n) for n in CASE_FIELDS).strip(),
        "links":       links,
        "resolution":  _value(f.get("resolution")),
    })
    return row


@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def incidents(project: str, start: date, end: date | None) -> list[dict]:
    """Every Production Incident created from `start` up to (not including)
    `end`; `end` None means up to now.  (Streamlit keys this cache on this
    function's source: a row gaining a field, like team_ownership, needs a
    change here too, or rows cached without it are served until tomorrow.)"""
    ids = _field_ids()
    jql = (f"project = {_q(project)} AND issuetype = {_q(INCIDENT_TYPE)} "
           f"AND created >= {_q(start.isoformat())}")
    if end is not None:
        jql += f" AND created < {_q(end.isoformat())}"
    fields = ("summary", "description", "issuetype", "created", "priority",
              "status", "components", "labels", "issuelinks", "resolution",
              *ids.values())
    raw = jira_client.search_all(jql + " ORDER BY created ASC", fields)
    base = _base_url()
    return [normalise_incident(i, base, ids) for i in raw]


# ── exclusions and metrics ───────────────────────────────────────────────────
def exclusion(row: dict) -> str | None:
    """Why Delivery's JQL leaves this incident out, or None if it counts."""
    if row.get("status", "").lower() in EXCLUDED_STATUSES:
        return "Cancelled"
    rc = (row.get("root_cause") or "").strip()
    if not rc:
        return "No root cause yet"
    if rc.lower() in EXCLUDED_ROOT_CAUSES:
        return f"Root cause: {rc}"
    app_components = [c for c in row.get("components", [])
                      if c.lower() in EXCLUDED_COMPONENTS]
    if app_components:
        return f"App component: {', '.join(app_components)}"
    app_labels = [lb for lb in row.get("labels", []) if lb.lower() in EXCLUDED_LABELS]
    if app_labels:
        return f"App label: {', '.join(app_labels)}"
    owners = [o.strip() for o in (row.get("team_ownership") or "").split(",")]
    app_owners = [o for o in owners if o.lower() in EXCLUDED_OWNERS]
    if app_owners:
        return f"App team: {', '.join(app_owners)}"
    return None


def split_uat(rows: list[dict], since: date | None) -> tuple[list[dict], list[dict]]:
    """(counted, left out): every Bug counts; a Defect counts when it was
    created since the previous release (`since`; None = no previous one).
    Left-out rows carry `excluded_because`, so the tab can list them."""
    counted, early = [], []
    for r in rows:
        created = r["created"].date() if r.get("created") else None
        if (r["type"] != "Defect" or since is None or created is None
                or created >= since):
            counted.append(r)
        else:
            early.append({**r, "excluded_because":
                          f"Defect created before the previous release ({since.isoformat()})"})
    return counted, early


def is_high(row: dict) -> bool:
    return row.get("priority") in HIGH_PRIORITIES


def ratio(leaked: int, uat: int) -> float | None:
    """None when there is nothing to divide by — a ratio over zero UAT issues
    would be arithmetic, not information."""
    return leaked / uat if uat else None


@dataclass
class ReleaseLeakage:
    group: Group
    release: Release
    window: Window | None
    uat: list[dict]
    leaks: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)   # rows + "excluded_because"
    uat_left_out: list[dict] = field(default_factory=list)   # older Defects
    previous: Release | None = None

    @property
    def uat_bugs(self) -> int:
        return sum(1 for r in self.uat if r["type"] == "Bug")

    @property
    def uat_defects(self) -> int:
        return sum(1 for r in self.uat if r["type"] == "Defect")

    @property
    def leaks_hh(self) -> int:
        return sum(1 for r in self.leaks if is_high(r))

    @property
    def ratio(self) -> float | None:
        return ratio(len(self.leaks), len(self.uat))

    @property
    def ratio_hh(self) -> float | None:
        return ratio(self.leaks_hh, len(self.uat))


def split(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(counted, excluded) — excluded rows carry `excluded_because`."""
    counted, excluded = [], []
    for r in rows:
        why = exclusion(r)
        if why is None:
            counted.append(r)
        else:
            excluded.append({**r, "excluded_because": why})
    return counted, excluded


def analyse(group: Group, rels: list[Release], name: str, today: date) -> ReleaseLeakage:
    """Everything the tab shows for one release (Jira reads are cached)."""
    rel = next(r for r in rels if r.name == name)
    win = window_for(rels, name, today)
    previous = previous_of(rels, rel)
    uat, early = split_uat(uat_issues(group.project, name),
                           previous.date if previous else None)
    if win is None:
        return ReleaseLeakage(group, rel, None, uat, uat_left_out=early, previous=previous)
    rows = incidents(group.project, win.start, None if win.open_ended else win.end)
    counted, excluded = split(rows)
    return ReleaseLeakage(group, rel, win, uat, counted, excluded, early, previous)


def trend(group: Group, rels: list[Release], today: date,
          n: int = 6) -> list[ReleaseLeakage]:
    """The last `n` shipped releases, oldest first."""
    shipped = [r for r in rels if r.released and r.date]
    return [analyse(group, rels, r.name, today) for r in shipped[-n:]]
