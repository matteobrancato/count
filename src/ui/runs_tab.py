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

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from .. import testrail_client as tr
from ..bu_rules import ALL_RULES, BU_RUN_ALIASES


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


def _bu_project_ids() -> dict[str, set[int]]:
    """Map BU → set of TestRail project IDs (derived from each BU's rule suites)."""
    out: dict[str, set[int]] = {}
    for r in ALL_RULES:
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
            t_fut = {pool.submit(tr.fetch_tests, rid): rid for rid in rids_with_bugs}
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
def _completed_runs_for_bu(bu: str, project_ids: set[int], limit: int) -> list[dict]:
    """Most recent N completed runs that match the BU (standalone + plan-contained).

    Plan detail fetches are parallelised — same reason as `_flatten_active_runs`:
    serial fetch_plan calls over a project with many plans is the dominant
    bottleneck on cold start.
    """
    candidates: list[dict] = []
    for pid in project_ids:
        # 1) Standalone completed runs (single paginated call).
        for run in tr.fetch_runs(pid, is_completed=True):
            if bu in _bus_for_run_name(run.get("name")):
                candidates.append({**run, "project_id": pid})

        # 2) Completed plans matching this BU — parallelise the detail fetches.
        matching_plans = [
            p for p in tr.fetch_plans(pid, is_completed=True)
            if bu in _bus_for_run_name(p.get("name"))
        ]
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

    # Parallel fetch get_tests for each run
    by_run: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=min(len(runs), 8)) as pool:
        futures = {pool.submit(tr.fetch_tests, int(r["id"])): int(r["id"]) for r in runs}
        for fut in as_completed(futures):
            rid = futures[fut]
            try:
                by_run[rid] = fut.result()
            except Exception:
                by_run[rid] = []

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
        return df
    return df.sort_values(["failure_rate", "fail"], ascending=[False, False]).reset_index(drop=True)


# ── UI sections ─────────────────────────────────────────────────────────────
_CLASS_ORDER = ["Always fail", "Flaky", "Always pass", "Insufficient data"]
_CLASS_EMOJI = {
    "Always fail":       "❌",
    "Flaky":             "🌀",
    "Always pass":       "✅",
    "Insufficient data": "·",
}


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

    # ── Runs table (sorted by recency) ─────────────────────────────────────
    df = pd.DataFrame(rows)
    st.dataframe(
        df[["name", "plan", "updated_str", "created_str", "days_idle",
            "total", "passed", "failed", "blocked",
            "completion", "pass_rate", "bugs_count", "url"]],
        use_container_width=True,
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

    # ── Bug detail table: bug ↔ test ↔ run ↔ date ──────────────────────────
    if not bug_records:
        return
    st.markdown("##### 🐛 Bug → Test linkage")
    st.caption(
        f"{n_unique_bugs} unique JIRA keys across {len(bug_records)} failure events. "
        "Each row is one logged failure; click the bug key to open it in JIRA."
    )
    bdf = pd.DataFrame(bug_records)
    st.dataframe(
        bdf[["bug_url", "case_id", "case_title", "run_name", "failed_str"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "bug_url":     st.column_config.LinkColumn(
                "Bug", display_text=r"[A-Z][A-Z0-9_]+-\d+", width="small"),
            "case_id":     st.column_config.NumberColumn("Test ID", width="small"),
            "case_title":  st.column_config.TextColumn("Test title", width="large"),
            "run_name":    st.column_config.TextColumn("Run", width="medium"),
            "failed_str":  st.column_config.TextColumn("Failed on", width="small"),
        },
    )


def _render_stability(bu: str, project_ids: set[int]) -> None:
    st.markdown("#### 📈 Test Stability")
    st.caption(
        "Classify cases by their result pattern across the last N **completed** runs. "
        "*Always fail* → fix priority · *Flaky* → investigate · *Always pass* → safe · "
        "*Insufficient data* → < 1 execution recorded."
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
        st.dataframe(rdf, use_container_width=True, hide_index=True)

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
        use_container_width=True,
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
@st.fragment
def render() -> None:
    st.subheader("🏃 Runs & Stability")
    st.caption(
        "Live view of active TestRail runs per BU, with bugs extracted from "
        "failed results, plus a stability classifier over recent completed runs."
    )

    # BU selector — all BUs that have at least one rule.
    all_bus = sorted({r.bu for r in ALL_RULES})
    bu = st.selectbox("Business Unit", all_bus, key="runs_bu")

    bu_to_pids = _bu_project_ids()
    project_ids = bu_to_pids.get(bu, set())
    if not project_ids:
        st.warning(f"Could not resolve TestRail project IDs for **{bu}**.")
        return

    base_url = tr.TestRailCredentials.from_secrets().base_url

    _render_active_runs(bu, project_ids, base_url)
    st.divider()
    _render_stability(bu, project_ids)
