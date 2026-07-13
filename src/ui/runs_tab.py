"""Runs tab — live status of active TestRail runs + test stability history.

Two sections, both filtered by the selected BU:

  1. **Active runs** — for each run currently open, show pass/fail counts,
     completion %, and the unique JIRA-like defect keys extracted from failed
     results' `defects` field (handles both raw "EE20-1234" keys and Jira links).

  2. **Stability history** — analyse the last *N* completed runs, classify each
     case as "always pass / always fail / flaky / insufficient data" based on
     its status pattern across those runs.  Highlights stuck failures (always
     fail) and flaky tests that pollute regression noise.

Data lives entirely in TestRail, fetched via the cached helpers in
`testrail_client.py`.  Active-run data has a 10-min TTL; completed-run data
has a 6-h TTL because results never change once a run is closed.
"""
from __future__ import annotations

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from .. import testrail_client as tr
from ..bu_rules import ALL_RULES, BU_RUN_ALIASES
from ..field_resolver import get_registry
from ..rules_engine import _get_multi_countries, _section_path_lookup
from . import global_filter
from .styles import COLORS


# ── constants ────────────────────────────────────────────────────────────────
# Standard TestRail status IDs.  Custom statuses (6+) are folded under "other".
_STATUS_PASSED   = 1
_STATUS_BLOCKED  = 2
_STATUS_UNTESTED = 3
_STATUS_RETEST   = 4
_STATUS_FAILED   = 5

# Extract JIRA-like keys "PROJ-123" from any string (including URLs).
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")

# JIRA base URL — used to render bug IDs as clickable links.
JIRA_BROWSE_URL = "https://elab-aswatson.atlassian.net/browse/"

# Pre-compute BU-alias regexes.  Multiple BUs can share an alias (e.g. "EE"
# matches Drogas, Watsons and Marionnaud) — the matcher returns ALL of them.
_BU_ALIAS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE), bu)
    for bu, aliases in BU_RUN_ALIASES.items()
    for alias in aliases
]


# ── BU matching ──────────────────────────────────────────────────────────────
def _bus_for_run_name(name: str | None) -> set[str]:
    """Return ALL BU display names whose aliases match the run/plan name.

    A single alias (e.g. "EE") can belong to multiple BUs, so the same run can
    legitimately surface under several of them.  Empty set = no match.
    """
    if not name:
        return set()
    return {bu for pattern, bu in _BU_ALIAS_PATTERNS if pattern.search(name)}


@st.cache_data(show_spinner=False, ttl=21600)
def _bu_project_ids(
    scopes: tuple[str, ...] = ("website", "next_gen"),
) -> dict[str, set[int]]:
    """Map BU → set of TestRail project IDs, derived from the BU's rule suites
    **restricted to the given scopes**.

    The scope filter matters: a BU like The Perfume Shop also has a dedicated
    mobile-app project — without the filter its MAPP runs matched the BU alias
    and leaked into the (web-focused) Runs tab.

    Cached: it loops every rule calling `resolve_project_id`, and is hit on every
    Runs-tab rerun plus every Dexter runs/bugs/stability tool call."""
    out: dict[str, set[int]] = {}
    for r in ALL_RULES:
        if r.scope not in scopes:
            continue
        try:
            pid = tr.resolve_project_id(r.suite_id)
        except Exception:
            continue
        out.setdefault(r.bu, set()).add(pid)
    return out


# ── timestamp helpers ───────────────────────────────────────────────────────
def _ts_to_date(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def _ts_to_datetime(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _days_since(ts: int | None) -> int | None:
    if not ts:
        return None
    delta = datetime.now(tz=timezone.utc) - datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return max(int(delta.days), 0)


# ── defect extraction ───────────────────────────────────────────────────────
def _extract_jira_keys(defects: str | None) -> list[str]:
    """Pull JIRA-style keys (e.g. EE20-1234) from raw defect fields or URLs."""
    if not defects:
        return []
    keys = _JIRA_KEY_RE.findall(defects)
    # Preserve first-seen order while deduplicating.
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


# ── run aggregation ─────────────────────────────────────────────────────────
def _flatten_active_runs(
    project_ids: set[int], bu: str | None = None,
) -> list[dict]:
    """Standalone runs + plan-contained runs for the given projects, active only.

    Each output dict carries the run + a synthetic ``plan_name`` and ``project_id``.
    Dedup on run ID so we don't show the same run twice if it ever appears in
    both get_runs and get_plan.entries.

    If *bu* is provided, we filter **at the source** by run/plan name — only
    fetching plan details for plans that match the BU.  This is the dominant
    cost on shared projects like MVP4: it has 100+ active plans across all BUs,
    but Drogas alone is only ~10 of them, so the filter cuts the cold-start
    fetch time by 80-90%.
    """
    def _matches_bu(name: str | None) -> bool:
        if bu is None:
            return True
        return bu in _bus_for_run_name(name)

    seen: set[int] = set()
    out:  list[dict] = []
    for pid in project_ids:
        # 1) Standalone runs (not inside a plan) — single paginated call.
        for run in tr.fetch_runs(pid, is_completed=False):
            if not _matches_bu(run.get("name")):
                continue
            rid = int(run.get("id"))
            if rid in seen:
                continue
            seen.add(rid)
            out.append({**run, "plan_name": None, "project_id": pid})

        # 2) Runs inside active plans — filter by plan name FIRST, then
        #    fetch details only for the matching subset (in parallel).
        plans = tr.fetch_plans(pid, is_completed=False)
        matching_plans = [p for p in plans if _matches_bu(p.get("name"))]
        if not matching_plans:
            continue
        plan_ids = [int(p["id"]) for p in matching_plans]
        plan_details: dict[int, dict] = {}
        max_workers = min(len(plan_ids), 10)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(tr.fetch_plan, plan_id): plan_id
                       for plan_id in plan_ids}
            for fut in as_completed(futures):
                plan_id = futures[fut]
                try:
                    plan_details[plan_id] = fut.result()
                except Exception:
                    plan_details[plan_id] = {}

        for plan in matching_plans:
            plan_id     = int(plan["id"])
            plan_detail = plan_details.get(plan_id, {})
            plan_name   = plan_detail.get("name") or plan.get("name")
            for entry in (plan_detail.get("entries") or []):
                for run in (entry.get("runs") or []):
                    if run.get("is_completed"):
                        continue
                    rid = int(run.get("id"))
                    if rid in seen:
                        continue
                    seen.add(rid)
                    out.append({**run, "plan_name": plan_name, "project_id": pid})
    return out


def _summarise_run(run: dict, base_url: str) -> dict:
    """Map a raw TestRail run dict to the columns the UI table expects."""
    passed   = int(run.get("passed_count")   or 0)
    failed   = int(run.get("failed_count")   or 0)
    blocked  = int(run.get("blocked_count")  or 0)
    untested = int(run.get("untested_count") or 0)
    retest   = int(run.get("retest_count")   or 0)
    # Sum any custom_status_*_count → "other"
    other = sum(
        int(v or 0) for k, v in run.items()
        if k.startswith("custom_status") and k.endswith("_count")
    )
    total       = passed + failed + blocked + untested + retest + other
    executed    = total - untested
    completion  = (executed / total * 100) if total else 0.0
    pass_rate   = (passed / executed * 100) if executed else 0.0
    created     = int(run.get("created_on") or 0) or None
    updated     = int(run.get("updated_on") or 0) or None
    return {
        "id":              int(run["id"]),
        "name":            run.get("name", "(unnamed)"),
        "plan":            run.get("plan_name") or "—",
        "url":             f"{base_url.rstrip('/')}/index.php?/runs/view/{int(run['id'])}",
        "total":           total,
        "passed":          passed,
        "failed":          failed,
        "blocked":         blocked,
        "untested":        untested,
        "retest":          retest,
        "completion":      round(completion, 1),
        "pass_rate":       round(pass_rate, 1),
        "created_on":      created,
        "updated_on":      updated,
        "created_str":     _ts_to_date(created),
        "updated_str":     _ts_to_datetime(updated),
        "days_idle":       _days_since(updated),
    }


def _collect_bug_records(runs: list[dict]) -> list[dict]:
    """One record per (bug, test, run, result) failure event.

    Two-stage fetch for speed: first pull failed results for every run in
    parallel (cheap-ish), then fetch the test list ONLY for runs that actually
    have defect-bearing failures.  On shared suites most active runs carry no
    open bugs, so this skips the bulk of the (expensive) get_tests calls.

    Records are sorted by failure date DESC so the most recent bug events surface first.
    """
    if not runs:
        return []

    name_by_rid = {int(r["id"]): r["name"] for r in runs}
    rids = list(name_by_rid.keys())

    # Stage 1 — failed results for every run, in parallel.
    results_by_run: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(len(rids), 12)) as pool:
        r_fut = {pool.submit(tr.fetch_failed_results, rid): rid for rid in rids}
        for fut in as_completed(r_fut):
            rid = r_fut[fut]
            try:
                results_by_run[rid] = fut.result()
            except Exception:
                results_by_run[rid] = []

    # Only runs whose failures actually reference a JIRA key need their tests.
    rids_with_bugs = [
        rid for rid, results in results_by_run.items()
        if any(_extract_jira_keys(res.get("defects")) for res in results)
    ]

    # Stage 2 — fetch tests only for those runs.
    tests_by_run: dict[int, list[dict]] = {}
    if rids_with_bugs:
        with ThreadPoolExecutor(max_workers=min(len(rids_with_bugs), 12)) as pool:
            # fetch_tests_fresh (10-min TTL): these are ACTIVE runs, whose test
            # list/statuses keep changing — the 6h completed-run TTL would show
            # stale bug→test links.
            t_fut = {pool.submit(tr.fetch_tests_fresh, rid): rid for rid in rids_with_bugs}
            for fut in as_completed(t_fut):
                rid = t_fut[fut]
                try:
                    tests_by_run[rid] = fut.result()
                except Exception:
                    tests_by_run[rid] = []

    records: list[dict] = []
    for rid in rids:
        # test_id → (case_id, title)
        lookup = {
            int(t["id"]): (int(t.get("case_id") or 0), t.get("title") or "")
            for t in tests_by_run.get(rid, [])
        }
        for res in results_by_run.get(rid, []):
            keys = _extract_jira_keys(res.get("defects"))
            if not keys:
                continue
            tid           = int(res.get("test_id") or 0)
            cid, title    = lookup.get(tid, (0, "(unknown test)"))
            failed_on     = int(res.get("created_on") or 0)
            for k in keys:
                records.append({
                    "bug":         k,
                    "bug_url":     f"{JIRA_BROWSE_URL}{k}",
                    "run_id":      rid,
                    "run_name":    name_by_rid[rid],
                    "case_id":     cid,
                    "case_title":  title,
                    "failed_on":   failed_on,
                    "failed_str":  _ts_to_datetime(failed_on),
                })

    records.sort(key=lambda r: (-r["failed_on"], r["bug"], -r["run_id"]))
    return records


# ── stability analysis ──────────────────────────────────────────────────────
def _completed_runs_for_bu(
    bu: str | None, project_ids: set[int], limit: int,
) -> list[dict]:
    """Most recent N completed runs (standalone + plan-contained).

    *bu* filters runs/plans by BU alias in the name; ``None`` keeps everything —
    used by the case deep-dive, which already scopes to the case's own project
    and must not drop runs whose names don't carry a BU alias.

    Plan detail fetches are parallelised — same reason as `_flatten_active_runs`:
    serial fetch_plan calls over a project with many plans is the dominant
    bottleneck on cold start.
    """
    def _matches(name: str | None) -> bool:
        return bu is None or bu in _bus_for_run_name(name)

    candidates: list[dict] = []
    for pid in project_ids:
        # 1) Standalone completed runs (single paginated call).
        for run in tr.fetch_runs(pid, is_completed=True):
            if _matches(run.get("name")):
                candidates.append({**run, "project_id": pid})

        # 2) Completed plans — parallelise the detail fetches, but only for the
        #    most recent ones: we keep `limit` runs at the end, so fetching
        #    details for hundreds of old plans is wasted work.
        matching_plans = sorted(
            (p for p in tr.fetch_plans(pid, is_completed=True)
             if _matches(p.get("name"))),
            key=lambda p: -int(p.get("completed_on") or p.get("created_on") or 0),
        )[:max(limit, 20)]
        if not matching_plans:
            continue
        plan_ids = [int(p["id"]) for p in matching_plans]
        max_workers = min(len(plan_ids), 10)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(tr.fetch_plan, plan_id): plan_id
                       for plan_id in plan_ids}
            for fut in as_completed(futures):
                try:
                    plan_detail = fut.result()
                except Exception:
                    continue
                for entry in (plan_detail.get("entries") or []):
                    for run in (entry.get("runs") or []):
                        if run.get("is_completed"):
                            candidates.append({**run, "project_id": pid})

    candidates.sort(key=lambda r: int(r.get("completed_on") or r.get("created_on") or 0),
                    reverse=True)
    return candidates[:limit]


def _classify_stability(runs: list[dict], min_executions: int = 5) -> pd.DataFrame:
    """For each case in *runs*, build a status pattern + classification.

    A case needs at least *min_executions* recorded pass/fail/retest results
    to receive a "real" classification — otherwise it's "Insufficient data".
    This avoids over-confident labels (e.g. one fail in a single run flagged
    as "Always fail") and is the single biggest knob to tune in this view.
    """
    if not runs:
        return pd.DataFrame()

    # Parallel fetch get_tests for each run.  Failed fetches are counted (and
    # surfaced via df.attrs so the caller can warn) instead of silently
    # classifying against a truncated history.
    by_run: dict[int, list[dict]] = {}
    n_fetch_failed = 0
    with ThreadPoolExecutor(max_workers=min(len(runs), 8)) as pool:
        futures = {pool.submit(tr.fetch_tests, int(r["id"])): int(r["id"]) for r in runs}
        for fut in as_completed(futures):
            rid = futures[fut]
            try:
                by_run[rid] = fut.result()
            except Exception:
                by_run[rid] = []
                n_fetch_failed += 1

    # Order runs chronologically (oldest → newest) so the status pattern reads left→right
    run_order = sorted(runs, key=lambda r: int(r.get("completed_on") or r.get("created_on") or 0))

    # Walk each case across all runs.  Build per-case status sequence.
    per_case: dict[int, dict] = {}
    for run in run_order:
        rid    = int(run["id"])
        tests  = by_run.get(rid, [])
        for t in tests:
            cid = int(t.get("case_id") or 0)
            if not cid:
                continue
            stat = int(t.get("status_id") or _STATUS_UNTESTED)
            entry = per_case.setdefault(cid, {
                "title":    t.get("title") or "",
                "pattern":  [],
                "pass":     0,
                "fail":     0,
                "blocked":  0,
                "untested": 0,
                "retest":   0,
            })
            entry["pattern"].append(stat)
            bucket = {
                _STATUS_PASSED:   "pass",
                _STATUS_FAILED:   "fail",
                _STATUS_BLOCKED:  "blocked",
                _STATUS_UNTESTED: "untested",
                _STATUS_RETEST:   "retest",
            }.get(stat)
            if bucket:
                entry[bucket] += 1

    icon = {
        _STATUS_PASSED: "✅", _STATUS_FAILED: "❌", _STATUS_BLOCKED: "🚫",
        _STATUS_UNTESTED: "·", _STATUS_RETEST: "🔄",
    }

    rows = []
    for cid, e in per_case.items():
        executed = e["pass"] + e["fail"] + e["retest"]
        if executed < min_executions:
            classification = "Insufficient data"
            failure_rate   = round(e["fail"] / executed * 100, 1) if executed else 0.0
        elif e["fail"] == 0:
            classification = "Always pass"
            failure_rate   = 0.0
        elif e["pass"] == 0 and e["fail"] > 0:
            classification = "Always fail"
            failure_rate   = 100.0
        else:
            classification = "Flaky"
            failure_rate   = round(e["fail"] / executed * 100, 1)

        rows.append({
            "case_id":        cid,
            "title":          e["title"],
            "pattern":        "".join(icon.get(s, "?") for s in e["pattern"]),
            "executions":     executed,
            "pass":           e["pass"],
            "fail":           e["fail"],
            "blocked":        e["blocked"],
            "classification": classification,
            "failure_rate":   failure_rate,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        df.attrs["n_fetch_failed"] = n_fetch_failed
        return df
    df = df.sort_values(["failure_rate", "fail"], ascending=[False, False]).reset_index(drop=True)
    df.attrs["n_fetch_failed"] = n_fetch_failed
    return df


# ── run-row rendering (TestRail-style visual list) ──────────────────────────
# Segment colours: passed / failed / blocked / retest / untested.
_BAR_COLORS = [
    ("passed",   "#16A34A"),
    ("failed",   "#DC2626"),
    ("blocked",  "#64748B"),
    ("retest",   "#F59E0B"),
    ("untested", "#E2E8F0"),
]


def _run_bar_html(r: dict) -> str:
    """Stacked result bar — one coloured segment per status, TestRail-style."""
    total = r["total"] or 1
    tip = html.escape(
        f"✅ {r['passed']:,} passed · ❌ {r['failed']:,} failed · "
        f"🚫 {r['blocked']:,} blocked · 🔁 {r['retest']:,} retest · "
        f"⚪ {r['untested']:,} untested — completion {r['completion']:.0f}%"
    )
    segs = "".join(
        f"<span style='width:{r[key] / total * 100:.2f}%;background:{color}'></span>"
        for key, color in _BAR_COLORS if r[key] > 0
    )
    return f"<div class='run-bar' title=\"{tip}\">{segs}</div>"


def _run_row_html(r: dict) -> str:
    """One run row: name/meta · stacked bar · passed % (+ bug badge)."""
    name = html.escape(r["name"])
    plan = html.escape(r["plan"] or "—")
    total = r["total"]
    pct_passed = (r["passed"] / total * 100) if total else 0.0
    idle = (f" · idle {r['days_idle']}d"
            if r["days_idle"] not in (None, 0) else "")
    bugs = r.get("bugs_count", 0)
    bug_html = f"<span class='run-bugchip'>🐛 {bugs}</span>" if bugs else ""
    return (
        f"<div class='run-row'>"
        f"<div class='run-info'>"
        f"<a href='{r['url']}' target='_blank'>{name}</a>"
        f"<div class='run-meta'>{plan} · created {r['created_str'] or '—'}"
        f" · last activity {r['updated_str'] or '—'}{idle}</div>"
        f"</div>"
        f"{_run_bar_html(r)}"
        f"<div class='run-right'><span class='run-pct'>{pct_passed:.0f}%</span>"
        f"<span class='run-sub'>{r['passed']:,}/{total:,} passed</span>{bug_html}</div>"
        f"</div>"
    )


# ── UI sections ─────────────────────────────────────────────────────────────
_CLASS_ORDER = ["Always fail", "Flaky", "Always pass", "Insufficient data"]
_CLASS_EMOJI = {
    "Always fail":       "❌",
    "Flaky":             "🌀",
    "Always pass":       "✅",
    "Insufficient data": "·",
}


@st.fragment
def _render_active_runs(bu: str, project_ids: set[int], base_url: str) -> None:
    st.markdown("#### 🏃 Active Runs")
    st.caption(
        "Non-completed TestRail runs matched to this BU by alias in the run/plan name. "
        "Sorted by last activity (most recent first)."
    )

    try:
        with st.spinner(f"⚡ Fetching {bu} runs from TestRail…"):
            # Pass bu filter so we skip fetching plan details for OTHER BUs.
            # This is the dominant cost on shared projects (MVP4 has 100+
            # active plans, only ~10 belong to any single BU).
            all_active = _flatten_active_runs(project_ids, bu=bu)
    except Exception as exc:                                            # noqa: BLE001
        st.error(
            f"⚠️ Could not fetch active runs: `{type(exc).__name__}: {str(exc)[:200]}`"
        )
        return

    # Defensive double-check (covers edge cases where a run lives inside a plan
    # whose name doesn't carry the BU but the run name itself does, or vice versa).
    bu_runs = [r for r in all_active
               if bu in _bus_for_run_name(r.get("name"))
               or bu in _bus_for_run_name(r.get("plan_name"))]
    if not bu_runs:
        st.info(f"No active runs found for **{bu}**.")
        return

    rows = [_summarise_run(r, base_url) for r in bu_runs]
    # Sort by last activity DESC (most recent first), fallback to created.
    rows.sort(key=lambda r: (-(r["updated_on"] or 0), -(r["created_on"] or 0)))

    # ── Bug records (we need them for the metric chip + the table below) ───
    bug_records = _collect_bug_records(rows)
    bugs_by_run: dict[int, set[str]] = {}
    for rec in bug_records:
        bugs_by_run.setdefault(rec["run_id"], set()).add(rec["bug"])
    for r in rows:
        r["bugs_count"] = len(bugs_by_run.get(r["id"], set()))

    # ── Top metric chips: temporal at-a-glance ─────────────────────────────
    n_runs       = len(rows)
    n_unique_bugs = len({rec["bug"] for rec in bug_records})
    most_recent  = max((r["updated_on"] or 0 for r in rows), default=0)
    oldest_open  = min((r["created_on"] or 0 for r in rows if r["created_on"]), default=0)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🏃 Active runs", f"{n_runs}")
    m2.metric("🐛 Open bugs (unique)", f"{n_unique_bugs}")
    m3.metric("⏱️ Most recent activity",
              _ts_to_date(most_recent) if most_recent else "—",
              help=_ts_to_datetime(most_recent))
    m4.metric("🕒 Oldest open since",
              _ts_to_date(oldest_open) if oldest_open else "—",
              help=f"Created {_days_since(oldest_open)} days ago"
              if oldest_open else None)

    # ── Runs list — TestRail-style visual rows ─────────────────────────────
    # Each row: name (link) + plan/dates, a stacked result bar (passed green,
    # failed red, blocked slate, retest amber, untested grey) and the passed %
    # — the at-a-glance read TestRail gives, plus bug badges it doesn't.
    flt = st.text_input(
        "Filter runs", key=f"runs_filter_{bu}",
        placeholder="🔎 Filter by run or plan name…",
        label_visibility="collapsed",
    )
    needle  = flt.strip().lower()
    visible = [r for r in rows
               if not needle
               or needle in (r["name"] or "").lower()
               or needle in (r["plan"] or "").lower()]
    if not visible:
        st.info("No runs match the filter.")
    shown = visible[:60]
    st.markdown(
        "<div class='run-list'>" + "".join(_run_row_html(r) for r in shown) + "</div>",
        unsafe_allow_html=True,
    )
    if len(visible) > len(shown):
        st.caption(f"Showing the {len(shown)} most recent of {len(visible)} runs — "
                   f"use the filter to narrow down.")

    with st.expander("📋 Table view (sortable columns)"):
        df = pd.DataFrame(rows)
        st.dataframe(
            df[["name", "plan", "updated_str", "created_str", "days_idle",
                "total", "passed", "failed", "blocked",
                "completion", "pass_rate", "bugs_count", "url"]],
            width="stretch",
            hide_index=True,
            column_config={
                "name":         st.column_config.TextColumn("Run", width="large"),
                "plan":         st.column_config.TextColumn("Plan", width="medium"),
                "updated_str":  st.column_config.TextColumn(
                    "Last activity", width="small",
                    help="When the last test result was logged."),
                "created_str":  st.column_config.TextColumn("Created", width="small"),
                "days_idle":    st.column_config.NumberColumn(
                    "Idle d", help="Days since last activity."),
                "total":        st.column_config.NumberColumn("Total"),
                "passed":       st.column_config.NumberColumn("✅"),
                "failed":       st.column_config.NumberColumn("❌"),
                "blocked":      st.column_config.NumberColumn("🚫"),
                "completion":   st.column_config.ProgressColumn(
                    "Completion", format="%.1f%%", min_value=0, max_value=100),
                "pass_rate":    st.column_config.ProgressColumn(
                    "Pass rate", format="%.1f%%", min_value=0, max_value=100),
                "bugs_count":   st.column_config.NumberColumn(
                    "🐛", help="Unique JIRA keys from failed results."),
                "url":          st.column_config.LinkColumn("Open", display_text="↗"),
            },
        )

    # ── Bug detail table: bug ↔ test ↔ run ↔ date (+ live Jira state) ──────
    if not bug_records:
        return
    st.markdown("##### 🐛 Bug → Test linkage")
    bdf = pd.DataFrame(bug_records)

    jira_info: dict[str, dict] = {}
    try:
        from .. import jira_client as jc
        if jc.available():
            jira_info = jc.fetch_issues(tuple(sorted({r["bug"] for r in bug_records})))
    except Exception:                                                   # noqa: BLE001
        jira_info = {}

    cols = ["bug_url", "case_id", "case_title", "run_name", "failed_str"]
    col_cfg = {
        "bug_url":     st.column_config.LinkColumn(
            # Capture group extracts the key from the URL (…/browse/EE20-1234);
            # a pattern WITHOUT a group makes Streamlit print the raw regex.
            "Bug", display_text=r"/browse/([A-Z][A-Z0-9_]+-\d+)", width="small"),
        "case_id":     st.column_config.NumberColumn("Test ID", width="small"),
        "case_title":  st.column_config.TextColumn("Test title", width="large"),
        "run_name":    st.column_config.TextColumn("Run", width="medium"),
        "failed_str":  st.column_config.TextColumn("Failed on", width="small"),
    }
    if jira_info:
        bdf["jira_status"] = bdf["bug"].map(
            lambda k: f"{jira_info[k]['glyph']} {jira_info[k]['status']}"
            if k in jira_info else "—")
        bdf["fix_versions"] = bdf["bug"].map(
            lambda k: ", ".join(jira_info[k]["fix_versions"]) or "—"
            if k in jira_info else "—")
        cols = ["bug_url", "jira_status", "fix_versions",
                "case_id", "case_title", "run_name", "failed_str"]
        col_cfg["jira_status"]  = st.column_config.TextColumn(
            "Jira status", width="small", help="Live from Jira (10-min cache).")
        col_cfg["fix_versions"] = st.column_config.TextColumn(
            "Fix version", width="small")
    st.caption(
        f"{n_unique_bugs} unique JIRA keys across {len(bug_records)} failure events. "
        "Each row is one logged failure; click the bug key to open it in JIRA."
        + (" Status and fix version are live from Jira." if jira_info else "")
    )
    st.dataframe(bdf[cols], width="stretch", hide_index=True,
                 column_config=col_cfg)


@st.fragment
def _render_stability(bu: str, project_ids: set[int]) -> None:
    st.markdown("#### 📈 Test Stability")
    st.caption(
        "Classify cases by their result pattern across the last N **completed** runs. "
        "*Always fail* → fix priority · *Flaky* → investigate · *Always pass* → safe · "
        "*Insufficient data* → fewer executions than the **Min executions** you set below."
    )

    c1, c2, _ = st.columns([1, 1, 3])
    n_runs = c1.selectbox(
        "Runs to analyse", [3, 5, 10, 20, 50], index=1,
        key=f"stab_n_{bu}",
        help="How many of the most recent completed runs to walk.",
    )
    min_exec = c2.number_input(
        "Min executions",
        min_value=1, max_value=int(n_runs),
        value=min(5, int(n_runs)),
        step=1, key=f"stab_min_exec_{bu}",
        help=("A case needs at least this many pass/fail/retest results to be "
              "classified.  Lower values surface more cases but are noisier."),
    )

    try:
        with st.spinner(
            f"Fetching last {n_runs} completed runs + their tests "
            "(can take 15-45s on cold start)…"
        ):
            runs = _completed_runs_for_bu(bu, project_ids, limit=int(n_runs))
            if not runs:
                st.info("No completed runs found for this BU.")
                return
            stab = _classify_stability(runs, min_executions=int(min_exec))
    except Exception as exc:                                            # noqa: BLE001
        st.error(
            f"⚠️ Could not fetch stability data: "
            f"`{type(exc).__name__}: {str(exc)[:200]}`"
        )
        return

    n_failed = int(stab.attrs.get("n_fetch_failed", 0))
    if n_failed:
        st.warning(
            f"⚠️ {n_failed} of {len(runs)} runs could not be fetched — "
            f"classifications below are based on a partial history."
        )
    if stab.empty:
        st.info("No test data found in the selected runs.")
        return

    # Date range of the analysed runs (temporal context)
    ts_for_sort = [int(r.get("completed_on") or r.get("created_on") or 0) for r in runs]
    earliest = min(ts_for_sort) if ts_for_sort else 0
    latest   = max(ts_for_sort) if ts_for_sort else 0
    st.caption(
        f"📅 Analysing **{len(runs)}** completed runs from "
        f"**{_ts_to_date(earliest)}** to **{_ts_to_date(latest)}** "
        f"(threshold: ≥ {min_exec} executions per case)."
    )

    # Summary chips: counts per classification
    counts = stab["classification"].value_counts().to_dict()
    chips = st.columns(len(_CLASS_ORDER))
    for col, cls in zip(chips, _CLASS_ORDER):
        col.metric(f"{_CLASS_EMOJI[cls]} {cls}", f"{counts.get(cls, 0):,}")

    # Optional drill: show which runs are in the analysis, newest first.
    with st.expander(f"📋 Analysed runs ({len(runs)})", expanded=False):
        ordered_runs = sorted(
            runs,
            key=lambda r: -int(r.get("completed_on") or r.get("created_on") or 0),
        )
        rdf = pd.DataFrame([{
            "Run":          r.get("name") or "(unnamed)",
            "Completed on": _ts_to_date(
                int(r.get("completed_on") or r.get("created_on") or 0)),
            "Passed":   int(r.get("passed_count")  or 0),
            "Failed":   int(r.get("failed_count")  or 0),
            "Blocked":  int(r.get("blocked_count") or 0),
        } for r in ordered_runs])
        st.dataframe(rdf, width="stretch", hide_index=True)

    # Filter by classification (default: hide always-pass to surface the noise)
    pick = st.multiselect(
        "Show classifications",
        _CLASS_ORDER,
        default=["Always fail", "Flaky"],
        key=f"stab_filter_{bu}",
    )
    if not pick:
        st.caption("Select at least one classification to see the table.")
        return

    sub = stab[stab["classification"].isin(pick)].copy()
    if sub.empty:
        st.info("No cases match the selected classifications.")
        return

    # Make the ID a direct link to the case in TestRail (opens that exact test).
    base_url = tr.TestRailCredentials.from_secrets().base_url.rstrip("/")
    sub["case_url"] = base_url + "/index.php?/cases/view/" + sub["case_id"].astype(str)

    st.dataframe(
        sub[["case_url", "title", "pattern", "executions", "pass", "fail",
             "blocked", "classification", "failure_rate"]],
        width="stretch",
        hide_index=True,
        column_config={
            "case_url":       st.column_config.LinkColumn(
                "ID", width="small", display_text=r"/cases/view/(\d+)",
                help="Open this test case in TestRail."),
            "title":          st.column_config.TextColumn("Title", width="large"),
            "pattern":        st.column_config.TextColumn(
                f"Pattern (last {len(runs)} runs)", width="medium",
                help="Each character is one run, oldest left → newest right."),
            "executions":     st.column_config.NumberColumn("Executions"),
            "pass":           st.column_config.NumberColumn("✅"),
            "fail":           st.column_config.NumberColumn("❌"),
            "blocked":        st.column_config.NumberColumn("🚫"),
            "classification": st.column_config.TextColumn("Classification"),
            "failure_rate":   st.column_config.ProgressColumn(
                "Failure rate", format="%.1f%%", min_value=0, max_value=100),
        },
    )
    st.caption(f"{len(sub):,} cases shown · {len(stab):,} total in analysis · "
               f"based on {len(runs)} completed runs")


# ── render ──────────────────────────────────────────────────────────────────
# ── in-depth single-case analysis ───────────────────────────────────────────
# status_id → (label, text colour, soft background, emoji)
_STATUS_DISPLAY: dict[int, tuple[str, str, str, str]] = {
    1: ("Passed",   "#16A34A", "#E6F6EC", "✅"),
    2: ("Blocked",  "#64748B", "#EEF1F6", "🚫"),
    3: ("Untested", "#94A3B8", "#F1F5F9", "⚪"),
    4: ("Retest",   "#D97706", "#FEF3E2", "🔁"),
    5: ("Failed",   "#DC2626", "#FCE7E7", "❌"),
}


def _status_disp(sid: int) -> tuple[str, str, str, str]:
    """(label, colour, bg, emoji) for a status id.

    Ids 1-5 use the styled built-ins; CUSTOM statuses (id ≥ 6) resolve their
    real TestRail label via the API instead of rendering as 'Status 11'."""
    sid = int(sid or 0)
    if sid in _STATUS_DISPLAY:
        return _STATUS_DISPLAY[sid]
    try:
        label = tr.fetch_statuses().get(sid, f"Status {sid}")
    except Exception:                                                   # noqa: BLE001
        label = f"Status {sid}"
    return (label, "#64748B", "#EEF1F6", "•")


def _parse_case_id(text: str) -> int | None:
    """Pull a case id from a TestRail case URL, a `C12345`, or a bare number."""
    if not text:
        return None
    m = re.search(r"/cases/view/(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\bC?0*(\d+)\b", text.strip())
    return int(m.group(1)) if m else None


def _case_field(case: dict, reg, label: str) -> str | None:
    """Resolve a custom dropdown / multi-select case field to its human label(s)."""
    try:
        meta = reg.field(label)
        if not meta:
            return None
        raw = case.get(meta.system_name)
        if raw in (None, "", []):
            return None
        if isinstance(raw, list):
            vals = [meta.values_by_id.get(int(v), str(v)) for v in raw if str(v).strip()]
            return ", ".join(v for v in vals if v) or None
        if isinstance(raw, bool):
            return "Yes" if raw else None
        if isinstance(raw, int):
            return meta.values_by_id.get(raw, str(raw))
        if isinstance(raw, str) and raw.strip().isdigit():
            return meta.values_by_id.get(int(raw.strip()), raw)
        return str(raw)
    except Exception:                                                   # noqa: BLE001
        return None


@st.cache_data(show_spinner=False, ttl=600)
def _case_run_universe(suite_id: int, depth: int) -> tuple[list[str], dict]:
    """All runs (active + recent completed) in the case's OWN project.

    In TestRail a run can only contain cases from its own project, so scanning
    the suite's project is both complete and minimal.  (Previously this walked
    every project mapped to the owning BUs — which pulled in unrelated projects,
    e.g. a BU's mobile-app one — and filtered runs by BU-alias in the name,
    silently dropping valid runs without an alias.)

    Returns (bus, {run_id: {name, plan, ts, url}}), capped to the `depth`
    most-recent runs so the per-run scan stays bounded.
    """
    bus = sorted({r.bu for r in ALL_RULES if r.suite_id == suite_id})
    base_url = tr.TestRailCredentials.from_secrets().base_url.rstrip("/")
    pid = tr.resolve_project_id(suite_id)
    meta: dict[int, dict] = {}
    for r in _flatten_active_runs({pid}):
        rid = int(r["id"])
        meta.setdefault(rid, {
            "name": r.get("name") or "(unnamed)",
            "plan": r.get("plan_name") or "—",
            "ts":   int(r.get("updated_on") or r.get("created_on") or 0),
            "url":  f"{base_url}/index.php?/runs/view/{rid}",
        })
    for r in _completed_runs_for_bu(None, {pid}, limit=depth):
        rid = int(r["id"])
        meta.setdefault(rid, {
            "name": r.get("name") or "(unnamed)",
            "plan": "—",
            "ts":   int(r.get("completed_on") or r.get("created_on") or 0),
            "url":  f"{base_url}/index.php?/runs/view/{rid}",
        })
    # Keep only the `depth` most-recent runs to bound the per-run scan.
    top = dict(sorted(meta.items(), key=lambda kv: -kv[1]["ts"])[:depth])
    return bus, top


def _gather_case_executions(
    case_id: int, run_ids: list[int],
) -> tuple[dict, dict, int]:
    """For each run, locate the case's test + pull its full result history.

    Two parallel passes (the second only over runs that actually contain the
    case), so we never fetch results for irrelevant runs.  Returns
    (found, history, n_failed) where *n_failed* counts runs whose fetch errored
    — surfaced as a warning so gaps aren't silently read as 'never ran'.
    Uses the fresh 10-min TTL: the scanned window mixes active runs.
    """
    found: dict[int, dict] = {}
    n_failed = 0
    if run_ids:
        with ThreadPoolExecutor(max_workers=min(len(run_ids), 16)) as pool:
            futs = {pool.submit(tr.fetch_tests_fresh, rid): rid for rid in run_ids}
            for fut in as_completed(futs):
                rid = futs[fut]
                try:
                    tests = fut.result()
                except Exception:                                       # noqa: BLE001
                    n_failed += 1
                    continue
                for t in tests:
                    if int(t.get("case_id") or 0) == case_id:
                        found[rid] = t
                        break

    history: dict[int, list[dict]] = {}
    if found:
        with ThreadPoolExecutor(max_workers=min(len(found), 16)) as pool:
            futs = {pool.submit(tr.fetch_results_for_case, rid, case_id): rid
                    for rid in found}
            for fut in as_completed(futs):
                rid = futs[fut]
                try:
                    history[rid] = fut.result()
                except Exception:                                       # noqa: BLE001
                    history[rid] = []
                    n_failed += 1
    return found, history, n_failed


def _render_case_header(case: dict, case_id: int, suite_id: int, base_url: str) -> None:
    """A clean header card: title, id link, key attributes, and what it covers."""
    title = case.get("title") or "(untitled)"
    case_url = f"{base_url}/index.php?/cases/view/{case_id}"

    # Resolve the readable attributes (all best-effort).
    try:
        reg = get_registry()
    except Exception:                                                   # noqa: BLE001
        reg = None
    type_name = prio_name = section = None
    pid: int | None = None
    try:
        types = {int(t["id"]): t.get("name") for t in tr.fetch_case_types()}
        type_name = types.get(int(case.get("type_id") or 0))
    except Exception:                                                   # noqa: BLE001
        pass
    try:
        prios = {int(p["id"]): p.get("name") for p in tr.fetch_priorities()}
        prio_name = prios.get(int(case.get("priority_id") or 0))
    except Exception:                                                   # noqa: BLE001
        pass
    try:
        pid = tr.resolve_project_id(suite_id)
        section = _section_path_lookup(tr.fetch_sections(pid, suite_id)).get(
            int(case.get("section_id") or 0))
    except Exception:                                                   # noqa: BLE001
        pass

    chips: list[tuple[str, str]] = []
    if type_name:
        chips.append(("Type", type_name))
    if prio_name:
        chips.append(("Priority", prio_name))
    if section:
        chips.append(("Area", section))
    if reg is not None:
        for lbl, key in [
            ("Automation", "Automation Status"),
            ("Testim Desktop", "Automation Status Testim Desktop"),
            ("Testim Mobile", "Automation Status Testim Mobile View"),
        ]:
            v = _case_field(case, reg, key)
            if v:
                chips.append((lbl, v))
        # Countries need the PROJECT-AWARE resolver: multi_countries has
        # different dropdown items per project, so the global id→label map
        # showed another project's labels (e.g. UK/IE rendered as KVBE/KVNL).
        try:
            tokens = _get_multi_countries(case, reg, pid)
            if tokens:
                chips.append(("Countries", ", ".join(tokens)))
        except Exception:                                               # noqa: BLE001
            pass

    chip_html = "".join(
        f"<span style='display:inline-flex;gap:6px;align-items:center;"
        f"background:{COLORS['canvas']};border:1px solid {COLORS['border']};"
        f"border-radius:8px;padding:4px 10px;font-size:12px'>"
        f"<span style='color:{COLORS['muted']};font-weight:600'>{k}</span>"
        f"<span style='color:{COLORS['text']};font-weight:600'>{v}</span></span>"
        for k, v in chips
    )

    # "Covers" — refs (stories / requirements), linked to JIRA when they look like keys.
    refs = (case.get("refs") or "").strip()
    covers_html = ""
    if refs:
        parts = [p.strip() for p in re.split(r"[,\s]+", refs) if p.strip()]
        links = []
        for p in parts:
            if _JIRA_KEY_RE.fullmatch(p):
                links.append(f"<a href='{JIRA_BROWSE_URL}{p}' target='_blank' "
                             f"style='color:{COLORS['brand']};font-weight:600'>{p}</a>")
            else:
                links.append(f"<span style='color:{COLORS['text']}'>{p}</span>")
        covers_html = (
            f"<div style='margin-top:10px;font-size:12.5px'>"
            f"<span style='color:{COLORS['muted']};font-weight:600'>Covers</span>"
            f"&nbsp;&nbsp;{' · '.join(links)}</div>"
        )

    st.markdown(
        f"<div style='background:{COLORS['surface']};border:1px solid {COLORS['border']};"
        f"border-radius:14px;padding:16px 18px;box-shadow:0 1px 3px rgba(15,23,42,0.05)'>"
        f"<div style='display:flex;align-items:baseline;gap:10px;flex-wrap:wrap'>"
        f"<a href='{case_url}' target='_blank' style='font-size:12px;font-weight:700;"
        f"color:#fff;background:{COLORS['brand']};border-radius:7px;padding:2px 9px;"
        f"text-decoration:none'>C{case_id} ↗</a>"
        f"<span style='font-size:18px;font-weight:750;color:{COLORS['ink']}'>{title}</span>"
        f"</div>"
        f"<div style='display:flex;flex-wrap:wrap;gap:7px;margin-top:12px'>{chip_html}</div>"
        f"{covers_html}"
        f"</div>",
        unsafe_allow_html=True,
    )


@st.fragment
def _render_release_readiness(bu: str, project_ids: set[int], base_url: str) -> None:
    """'Is the release ready?' — TestRail's latest regression run joined with
    Jira's fix-version state.  The TestRail half always works; the Jira half
    appears only when the Atlassian secrets are configured."""
    st.markdown("#### 🚦 Release readiness")
    st.caption(
        "The latest **completed regression run** for this BU, and — pick a Jira "
        "fix version — how close its scope is to done."
    )

    # ── TestRail half: latest completed run with 'regr' in the name ─────────
    try:
        completed = _completed_runs_for_bu(bu, project_ids, limit=30)
    except Exception:                                                   # noqa: BLE001
        completed = []
    regr = [r for r in completed if "regr" in (r.get("name") or "").lower()]
    if regr:
        row = _summarise_run(regr[0], base_url)
        row["bugs_count"] = 0            # not computed here — keep the row light
        st.markdown(f"<div class='run-list'>{_run_row_html(row)}</div>",
                    unsafe_allow_html=True)
    else:
        st.info("No completed regression run found in the recent history.")

    # ── Jira half: fix-version scope state ──────────────────────────────────
    try:
        from .. import jira_client as jc
        jira_ok = jc.available()
    except Exception:                                                   # noqa: BLE001
        jira_ok = False
    if not jira_ok:
        st.caption("🔌 Configure the Atlassian secrets to see fix-version "
                   "readiness from Jira here.")
        return

    c1, c2 = st.columns([1, 2], vertical_alignment="bottom")
    projects = jc.fetch_projects()
    if projects:
        labels = [f"{p['key']} — {p['name']}" for p in projects]
        chosen_proj = c1.selectbox(
            "Jira project", labels, key="rr_project",
            index=None, placeholder="Choose a project…",
            help="The Jira project whose fix versions you want to check.")
        if not chosen_proj:
            return
        project_key = projects[labels.index(chosen_proj)]["key"]
    else:
        # Fallback (no project list / permission): free-text key entry.
        project_key = c1.text_input(
            "Jira project key", key="rr_project_txt", placeholder="e.g. EE20",
            help="The prefix of the bug keys (EE20-1234 → EE20).",
        ).strip().upper()
        if not project_key:
            return
    versions = jc.fetch_versions(project_key)
    if not versions:
        c2.warning(f"No versions found for Jira project **{project_key}** "
                   f"(check the key and your Jira permissions).")
        return
    names = [v["name"] for v in versions]
    chosen = c2.selectbox("Fix version", names, key=f"rr_version_{project_key}")
    ver = next(v for v in versions if v["name"] == chosen)

    base_jql  = f'project = "{project_key}" AND fixVersion = "{chosen}"'
    n_total   = jc.count_issues(base_jql)
    n_done    = jc.count_issues(base_jql + " AND statusCategory = Done")
    n_bugs    = jc.count_issues(
        base_jql + " AND issuetype = Bug AND statusCategory != Done")

    if n_total is None:
        st.warning("Couldn't query Jira for this version right now.")
        return
    done_pct = (n_done / n_total * 100) if (n_total and n_done is not None) else 0.0

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Issues in scope", f"{n_total:,}")
    m2.metric("Done", f"{n_done:,}" if n_done is not None else "—",
              delta=f"{done_pct:.0f}%", delta_color="off")
    m3.metric("Open bugs", f"{n_bugs:,}" if n_bugs is not None else "—")
    m4.metric("Release date", ver["release_date"] or "—",
              help="Planned release date from Jira"
                   + (" · already released" if ver["released"] else ""))

    # Simple, honest verdict — same RAG language as the rest of the dashboard.
    if (n_bugs == 0) and done_pct >= 95:
        st.success(f"🟢 **{chosen} looks ready** — {done_pct:.0f}% done, "
                   f"no open bugs in scope.")
    elif (n_bugs is not None and n_bugs <= 3) and done_pct >= 80:
        st.warning(f"🟡 **{chosen} is close** — {done_pct:.0f}% done, "
                   f"{n_bugs} open bug{'s' if n_bugs != 1 else ''} left.")
    else:
        st.error(f"🔴 **{chosen} is not ready** — {done_pct:.0f}% done, "
                 f"{n_bugs if n_bugs is not None else '?'} open bugs in scope.")


@st.fragment
def _render_case_deep_dive() -> None:
    st.markdown("#### 🔬 In-depth Test Analysis")
    st.caption(
        "The full story of **one test case**: every recent run it appeared in and "
        "the status it ended with, how many times it was executed, the JIRA bugs "
        "it raised over time, and the story/requirement it covers."
    )

    c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
    raw = c1.text_input(
        "🔗 Test case URL or ID", key="deep_case_input",
        placeholder="Paste the case link from TestRail…  e.g. …/index.php?/cases/view/3500712, C3500712 or 3500712",
        help="Open the test case in TestRail and copy the address bar URL — "
             "or just type its ID (with or without the leading C).",
    )
    depth = int(c2.number_input(
        "Runs to scan", min_value=20, max_value=400, value=80, step=20,
        key="deep_depth",
        help="How far back to look: the N most-recent runs of the case's "
             "project. Increase it if an older run you expect is missing."))

    case_id = _parse_case_id(raw)
    if not case_id:
        if raw.strip():
            st.warning("Couldn't read a case ID — use the full case URL or e.g. `C3500712`.")
        else:
            st.caption("💡 Try it with any case from the stability table above — "
                       "paste its ID to see its full execution history.")
        return

    try:
        case = tr.fetch_case(case_id)
    except Exception as exc:                                            # noqa: BLE001
        st.error(f"Couldn't load case **C{case_id}**: "
                 f"`{type(exc).__name__}: {str(exc)[:160]}`")
        return

    suite_id = int(case.get("suite_id") or 0)
    base_url = tr.TestRailCredentials.from_secrets().base_url.rstrip("/")

    with st.spinner(f"Scanning runs for C{case_id} (can take a few seconds)…"):
        bus, run_meta = _case_run_universe(suite_id, depth)
        found, history, n_failed = _gather_case_executions(
            case_id, list(run_meta.keys()))

    _render_case_header(case, case_id, suite_id, base_url)

    if n_failed:
        st.warning(
            f"⚠️ {n_failed} of {len(run_meta)} runs could not be fetched — "
            f"the history below may be incomplete."
        )
    if not found:
        st.info(
            f"This case wasn't found in the {len(run_meta)} most-recent runs "
            f"for {', '.join(bus) or 'its BU'}. Increase **Runs to scan** to look deeper."
        )
        return

    # Build per-run records + collect the bug history.
    records: list[dict] = []
    all_bugs: list[tuple[str, int, str]] = []   # (key, ts, run_name)
    for rid, test in found.items():
        meta    = run_meta[rid]
        results = history.get(rid, [])
        # Honest count: None (blank cell) when the results fetch returned
        # nothing, instead of fabricating "1".
        n_exec  = sum(1 for r in results if int(r.get("status_id") or 0)) or None
        run_bugs: set[str] = set()
        for r in results:
            for k in _extract_jira_keys(r.get("defects")):
                run_bugs.add(k)
                all_bugs.append((k, int(r.get("created_on") or meta["ts"] or 0), meta["name"]))
        label, _c, _b, emoji = _status_disp(int(test.get("status_id") or 0))
        records.append({
            "Run":        meta["name"],
            "Plan":       meta["plan"],
            "Final status": f"{emoji} {label}",
            "Last run":   _ts_to_date(meta["ts"]),
            "Times run":  n_exec,
            "Bugs":       ", ".join(sorted(run_bugs)) or "—",
            "Open":       meta["url"],
            "_ts":        meta["ts"],
            "_pass":      int(test.get("status_id") or 0) == _STATUS_PASSED,
        })
    records.sort(key=lambda r: -r["_ts"])

    n_runs    = len(records)
    n_pass    = sum(1 for r in records if r["_pass"])
    pass_rate = n_pass / n_runs * 100 if n_runs else 0.0
    uniq_bugs = sorted({k for k, _, _ in all_bugs})

    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Runs found", f"{n_runs:,}")
    s2.metric("Final pass rate", f"{pass_rate:.0f}%",
              help="Share of runs where the case ended Passed.")
    s3.metric("Open bugs (history)", f"{len(uniq_bugs):,}")
    s4.metric("Latest status", records[0]["Final status"],
              help=f"{records[0]['Run']} · {records[0]['Last run']}")

    st.markdown("##### 🗓 Execution history")
    df = pd.DataFrame(records).drop(columns=["_ts", "_pass"])
    st.dataframe(
        df, width="stretch", hide_index=True,
        column_config={
            "Run":          st.column_config.TextColumn("Run", width="large"),
            "Plan":         st.column_config.TextColumn("Plan", width="medium"),
            "Final status": st.column_config.TextColumn("Final status", width="small"),
            "Last run":     st.column_config.TextColumn("Last run", width="small"),
            "Times run":    st.column_config.NumberColumn("Times run", width="small"),
            "Bugs":         st.column_config.TextColumn("Bugs", width="medium"),
            "Open":         st.column_config.LinkColumn("Open", display_text="↗", width="small"),
        },
    )

    if uniq_bugs:
        st.markdown("##### 🐞 Bug history")
        latest: dict[str, tuple[int, str]] = {}
        for k, ts, rname in all_bugs:
            if k not in latest or ts > latest[k][0]:
                latest[k] = (ts, rname)
        # Live Jira state on each chip (best-effort — chips render without it).
        jira_info: dict[str, dict] = {}
        try:
            from .. import jira_client as jc
            if jc.available():
                jira_info = jc.fetch_issues(tuple(uniq_bugs))
        except Exception:                                               # noqa: BLE001
            jira_info = {}

        def _chip_state(k: str) -> str:
            info = jira_info.get(k)
            if not info:
                return ""
            fx = f" → {', '.join(info['fix_versions'])}" if info["fix_versions"] else ""
            return (f"<span style='color:{COLORS['text']};font-weight:600'>"
                    f"· {info['glyph']} {info['status']}{fx}</span>")

        chips = "".join(
            f"<a href='{JIRA_BROWSE_URL}{k}' target='_blank' "
            f"style='display:inline-flex;align-items:center;gap:6px;margin:0 6px 6px 0;"
            f"background:{COLORS['surface']};border:1px solid {COLORS['border_2']};"
            f"border-radius:999px;padding:4px 11px;font-size:12px;font-weight:600;"
            f"color:{COLORS['brand']};text-decoration:none'>🐞 {k}"
            f"<span style='color:{COLORS['muted']};font-weight:500'>"
            f"· {_ts_to_date(latest[k][0])}</span>{_chip_state(k)}</a>"
            for k in uniq_bugs
        )
        st.markdown(f"<div>{chips}</div>", unsafe_allow_html=True)


# Process-wide marker of (scope, bu) whose live-runs data was fetched recently.
# When fresh (within the runs TTL) the sections auto-render on cache hits;
# otherwise they load ON DEMAND behind an explicit button — the 30-50s live
# TestRail burst used to run eagerly on EVERY full rerun, keeping the browser's
# "running" spinner alive long after the page looked ready (the exact symptom
# management saw), and stretching the window where a dropped websocket forces
# a refresh.
_RUNS_WARM: dict[tuple[str, str], float] = {}
_RUNS_WARM_TTL = 540    # seconds — just under the 600s TTL of the run fetchers


@st.fragment
def render() -> None:
    st.subheader("🏃 Runs & Stability")
    st.caption(
        "Live view of active TestRail runs per BU, with bugs extracted from "
        "failed results, plus a stability classifier over recent completed runs."
    )

    # Scope + BU come from the GLOBAL control bar.  The scope maps 1:1 to
    # TestRail projects — web and mobile-app runs live in DIFFERENT projects
    # (e.g. TPS has a dedicated MAPP project), so mixing scopes under one BU
    # pulled mobile runs into the web view.
    scope, bu = global_filter.current()
    if not bu:
        st.info("No Business Units in this scope.")
        return
    st.caption(f"Showing **{bu}** · {global_filter.scope_label(scope)}")

    project_ids = _bu_project_ids((scope,)).get(bu, set())
    if not project_ids:
        st.warning(f"Could not resolve TestRail project IDs for **{bu}**.")
        return

    base_url = tr.TestRailCredentials.from_secrets().base_url

    warm = (time.time() - _RUNS_WARM.get((scope, bu), 0.0)) < _RUNS_WARM_TTL
    if not (warm or st.session_state.get(f"runs_go_{scope}_{bu}")):
        st.info(
            "**Live runs data loads on demand** — it's the only section that "
            "queries TestRail live, and the first load for a BU takes "
            "~30-45 seconds. Everything else on this page stays instant."
        )
        if st.button("⚡ Load live runs, stability & release readiness",
                     key=f"runs_load_{scope}_{bu}", type="primary"):
            st.session_state[f"runs_go_{scope}_{bu}"] = True
            st.rerun()
        # The case deep-dive is URL-driven and independent of the runs data.
        st.divider()
        _render_case_deep_dive()
        return

    _render_active_runs(bu, project_ids, base_url)
    st.divider()
    _render_stability(bu, project_ids)
    st.divider()
    _render_release_readiness(bu, project_ids, base_url)
    st.divider()
    _render_case_deep_dive()
    _RUNS_WARM[(scope, bu)] = time.time()
