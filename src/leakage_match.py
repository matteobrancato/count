"""Which TestRail case should have caught a production incident.

Built only from TestRail data the dashboard has already downloaded (the
website scope's cases, with title, section and References), so matching costs
no TestRail request.  In order of trust:

1. Direct links — deterministic, shown as "linked":
     * a case whose References cite the incident;
     * a case named in the incident's "TestRail Case ID" / "Test Case
       Reference" field;
     * a case whose References cite an issue linked to the incident (the
       story or bug it was cloned from, or relates to).
2. Candidates — the cases whose title and section share the most
   distinctive words with the incident (TF-IDF).  Only candidates: the AI
   picks among them and says how sure it is, and a low-confidence pick is
   never presented as coverage.

A case's coverage status comes from the Backlog tab's own classification, so
"automated" means here exactly what it means there.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from . import jira_client
from .freshness import DAY_TTL

TOP_CANDIDATES = 10
# The AI's confidence at or above which a match counts as the covering case.
LIKELY = 0.6

AUTOMATED = "Automated"
OUTDATED = "Automated, to be updated"
MANUAL = "Manual, in regression"
MANUAL_OUT = "Manual, not in regression"

_BASELINE_LABELS = ("big_regr_desktop", "big_regr_mobile")
_WORD = re.compile(r"[a-z][a-z0-9]{2,}")
_STOP = frozenset((
    "the", "and", "for", "with", "that", "this", "from", "into", "when", "then",
    "than", "are", "was", "were", "been", "have", "has", "not", "but", "can", "cannot",
    "could", "should", "would", "will", "all", "any", "its", "our", "your",
    "their", "them", "they", "there", "here", "which", "what", "who", "why", "how",
    "per", "via", "out", "off", "one", "two", "some", "such", "only", "also", "more",
    "most", "very", "page", "user", "users", "customer", "customers", "test", "case",
    "cases", "check", "verify", "able", "unable", "shown", "show", "shows",
    "showing", "displayed", "display", "error", "issue", "issues", "prod",
    "production", "uat", "site", "website", "web", "www", "https", "http", "com",
    "html", "browse", "jira",
))


@dataclass(frozen=True)
class CaseInfo:
    case_id: int
    title: str
    section: str
    url: str
    status: str                  # AUTOMATED / OUTDATED / MANUAL / MANUAL_OUT


def tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def _status(case_id: int, automated: set[int], outdated: set[int],
            baseline: set[int]) -> str:
    if case_id in outdated:
        return OUTDATED
    if case_id in automated:
        return AUTOMATED
    return MANUAL if case_id in baseline else MANUAL_OUT


@dataclass
class Index:
    cases: dict[int, CaseInfo]
    refs: dict[str, set[int]]                # Jira key → citing case ids
    toks: dict[int, Counter]
    idf: dict[str, float]
    norm: dict[int, float]


def build_index(cases: pd.DataFrame, automated: set[int], outdated: set[int]) -> Index:
    """`cases`: case_id, title, section_path, url, refs, labels."""
    infos: dict[int, CaseInfo] = {}
    refs: dict[str, set[int]] = {}
    toks: dict[int, Counter] = {}
    df: Counter = Counter()
    baseline = {int(r.case_id) for r in cases.itertuples()
                if any(lb in (r.labels or []) for lb in _BASELINE_LABELS)}
    for r in cases.drop_duplicates("case_id").itertuples():
        cid = int(r.case_id)
        section = str(r.section_path or "")
        infos[cid] = CaseInfo(cid, str(r.title or ""), section, str(r.url or ""),
                              _status(cid, automated, outdated, baseline))
        for key in jira_client.extract_issue_keys(str(r.refs or "")):
            refs.setdefault(key, set()).add(cid)
        c = Counter(tokens(f"{r.title} {section.replace('>', ' ')}"))
        toks[cid] = c
        df.update(c.keys())
    n = max(1, len(infos))
    idf = {t: math.log((n + 1) / (k + 1)) + 1.0 for t, k in df.items()}
    norm = {cid: math.sqrt(sum((idf[t] * v) ** 2 for t, v in c.items())) or 1.0
            for cid, c in toks.items()}
    return Index(infos, refs, toks, idf, norm)


def incident_text(row: dict) -> str:
    return " ".join([row.get("summary", "")] * 2 + [      # the title weighs double
        row.get("description", ""), row.get("steps", ""), row.get("expected", ""),
        " ".join(row.get("components", []))])


# "C123" at any length; a bare number only from 5 digits, so a year or a
# quantity in the same field is never read as a case.
_CASE_ID = re.compile(r"\bC(\d{1,9})\b|\b(\d{5,9})\b", re.IGNORECASE)


def direct_links(row: dict, index: Index) -> list[tuple[CaseInfo, str]]:
    """(case, why) for every deterministic link, most direct first."""
    out: dict[int, str] = {}
    for cid in index.refs.get(row["key"], ()):
        out.setdefault(cid, f"TestRail case cites {row['key']} in References")
    for m in _CASE_ID.finditer(row.get("case_refs", "")):
        cid = int(m.group(1) or m.group(2))
        if cid in index.cases:
            out.setdefault(cid, "Named in the incident's TestRail Case ID field")
    for link in row.get("links", []):
        for cid in index.refs.get(link["key"], ()):
            out.setdefault(cid, f"TestRail case cites {link['key']}, "
                                f"{link['relation'] or 'linked'} to the incident")
    return [(index.cases[c], why) for c, why in out.items()]


def candidates(row: dict, index: Index, exclude: set[int],
               k: int = TOP_CANDIDATES) -> list[CaseInfo]:
    """The k cases most similar to the incident (cosine over TF-IDF)."""
    q = Counter(t for t in tokens(incident_text(row)) if t in index.idf)
    if not q:
        return []
    qn = math.sqrt(sum((index.idf[t] * v) ** 2 for t, v in q.items())) or 1.0
    scores: list[tuple[float, int]] = []
    for cid, c in index.toks.items():
        if cid in exclude:
            continue
        shared = q.keys() & c.keys()
        if not shared:
            continue
        s = sum(index.idf[t] ** 2 * q[t] * c[t] for t in shared)
        scores.append((s / (qn * index.norm[cid]), cid))
    scores.sort(reverse=True)
    return [index.cases[cid] for _s, cid in scores[:k]]


def gap(statuses: list[str]) -> str:
    """The coverage gap a UAT-detectable leak points at, from the status of
    the case(s) that should have caught it."""
    if not statuses:
        return "No test case"
    automated = any(s in (AUTOMATED, OUTDATED) for s in statuses)
    manual = any(s in (MANUAL, MANUAL_OUT) for s in statuses)
    if automated and manual:
        return "Manual and automated"
    if automated:
        return ("Automated test outdated" if all(s == OUTDATED for s in statuses
                                                 if s in (AUTOMATED, OUTDATED))
                else "Automated test")
    if all(s == MANUAL_OUT for s in statuses):
        return "Manual, not in regression"
    return "Manual test"


# ── the index for a group, from the Backlog tab's cached frames ──────────────
@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def index_for(bus: tuple[str, ...]) -> Index:
    """Cases of the given BUs (website scope), with their Backlog status."""
    from .ui.backlog_tab import _backlog_data, _filter_bu, _load_scope

    raw, auto, rules = _load_scope("website")
    frames, automated, outdated = [], set(), set()
    _summary, expanded_by_bu, _auto_by_bu = _backlog_data()
    for bu in bus:
        r, a, _rules = _filter_bu(raw, auto, rules, bu)
        if not r.empty:
            frames.append(r)
        if not a.empty:
            automated |= set(a["case_id"].astype(int))
        exp = expanded_by_bu.get((bu, "website"))
        if exp is not None and not exp.empty:
            outdated |= set(exp.loc[exp["category"] == "to_be_updated", "case_id"].astype(int))
    cols = ["case_id", "title", "section_path", "url", "refs", "labels"]
    cases = (pd.concat(frames)[cols] if frames else pd.DataFrame(columns=cols))
    return build_index(cases, automated, outdated)
