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

# Pre-compute BU-alias regexes (longer aliases first → "TPS" wins over "TP").
_BU_ALIAS_PATTERNS: list[tuple[re.Pattern[str], str]] = sorted(
    (
        (re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE), bu)
        for bu, aliases in BU_RUN_ALIASES.items()
        for alias in aliases
    ),
    key=lambda x: -len(x[0].pattern),
)


# ── BU matching ──────────────────────────────────────────────────────────────
def _bu_for_run_name(name: str | None) -> str | None:
    """Return the BU display name matched by aliases in a run/plan name, else None."""
    if not name:
        return None
    for pattern, bu in _BU_ALIAS_PATTERNS:
        if pattern.search(name):
            return bu
    return None


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
def _flatten_active_runs(project_ids: set[int]) -> list[dict]:
    """Standalone runs + plan-contained runs for the given projects, active only.

    Each output dict carries the run + a synthetic ``plan_name`` and ``project_id``.
    Dedup on run ID so we don't show the same run twice if it ever appears in
    both get_runs and get_plan.entries.
    """
    seen: set[int] = set()
    out:  list[dict] = []
    for pid in project_ids:
        # 1) Standalone runs (not inside a plan)
        for run in tr.fetch_runs(pid, is_completed=False):
            rid = int(run.get("id"))
            if rid in seen:
                continue
            seen.add(rid)
            out.append({**run, "plan_name": None, "project_id": pid})
        # 2) Runs inside active plans
        for plan in tr.fetch_plans(pid, is_completed=False):
            plan_detail = tr.fetch_plan(int(plan["id"]))
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
    return {
        "id":         int(run["id"]),
        "name":       run.get("name", "(unnamed)"),
        "plan":       run.get("plan_name") or "—",
        "url":        f"{base_url.rstrip('/')}/index.php?/runs/view/{int(run['id'])}",
        "total":      total,
        "passed":     passed,
        "failed":     failed,
        "blocked":    blocked,
        "untested":   untested,
        "retest":     retest,
        "completion": round(completion, 1),
        "pass_rate":  round(pass_rate, 1),
    }


def _bugs_for_runs(run_ids: list[int]) -> dict[int, list[str]]:
    """Parallel fetch + extract JIRA keys for each run's failed results."""
    out: dict[int, list[str]] = {rid: [] for rid in run_ids}
    if not run_ids:
        return out
    with ThreadPoolExecutor(max_workers=min(len(run_ids), 8)) as pool:
        futures = {pool.submit(tr.fetch_failed_results, rid): rid for rid in run_ids}
        for fut in as_completed(futures):
            rid = futures[fut]
            try:
                results = fut.result()
            except Exception:
                results = []
            keys: set[str] = set()
            for res in results:
                keys.update(_extract_jira_keys(res.get("defects")))
            out[rid] = sorted(keys)
    return out


# ── stability analysis ──────────────────────────────────────────────────────
def _completed_runs_for_bu(bu: str, project_ids: set[int], limit: int) -> list[dict]:
    """Most recent N completed runs that match the BU (standalone + plan-contained)."""
    candidates: list[dict] = []
    for pid in project_ids:
        for run in tr.fetch_runs(pid, is_completed=True):
            if _bu_for_run_name(run.get("name")) == bu:
                candidates.append({**run, "project_id": pid})
        for plan in tr.fetch_plans(pid, is_completed=True):
            if _bu_for_run_name(plan.get("name")) != bu:
                continue
            plan_detail = tr.fetch_plan(int(plan["id"]))
            for entry in (plan_detail.get("entries") or []):
                for run in (entry.get("runs") or []):
                    if run.get("is_completed"):
                        candidates.append({**run, "project_id": pid})
    candidates.sort(key=lambda r: int(r.get("completed_on") or r.get("created_on") or 0),
                    reverse=True)
    return candidates[:limit]


def _classify_stability(runs: list[dict]) -> pd.DataFrame:
    """For each case in *runs*, build a status pattern + classification."""
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
        if executed == 0:
            classification = "Insufficient data"
            failure_rate   = 0.0
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
        "All non-completed runs matched to this BU by the run/plan name "
        "(e.g. `SD`, `WTR`, `TPS`).  Pass/fail counts come straight from TestRail."
    )

    with st.spinner("Fetching active runs…"):
        all_active = _flatten_active_runs(project_ids)

    bu_runs = [r for r in all_active if _bu_for_run_name(r.get("name")) == bu
               or _bu_for_run_name(r.get("plan_name")) == bu]
    if not bu_runs:
        st.info(f"No active runs found for **{bu}**.")
        return

    rows  = [_summarise_run(r, base_url) for r in bu_runs]
    bugs  = _bugs_for_runs([r["id"] for r in rows])
    for r in rows:
        r["bugs"]       = bugs.get(r["id"], [])
        r["bugs_count"] = len(r["bugs"])

    df = pd.DataFrame(rows).sort_values("completion", ascending=False)

    st.dataframe(
        df[["name", "plan", "total", "passed", "failed", "blocked", "untested",
            "completion", "pass_rate", "bugs_count", "url"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "name":       st.column_config.TextColumn("Run", width="large"),
            "plan":       st.column_config.TextColumn("Plan",  width="medium"),
            "total":      st.column_config.NumberColumn("Total"),
            "passed":     st.column_config.NumberColumn("✅ Passed"),
            "failed":     st.column_config.NumberColumn("❌ Failed"),
            "blocked":    st.column_config.NumberColumn("🚫 Blocked"),
            "untested":   st.column_config.NumberColumn("· Untested"),
            "completion": st.column_config.ProgressColumn(
                "Completion", format="%.1f%%", min_value=0, max_value=100),
            "pass_rate":  st.column_config.ProgressColumn(
                "Pass rate", format="%.1f%%", min_value=0, max_value=100),
            "bugs_count": st.column_config.NumberColumn(
                "🐛 Bugs", help="Unique JIRA keys extracted from failed results' defect field."),
            "url":        st.column_config.LinkColumn("Open", display_text="↗"),
        },
    )

    # Bug detail expander — shows the actual keys per run
    runs_with_bugs = [r for r in rows if r["bugs"]]
    if runs_with_bugs:
        with st.expander(f"🐛 Bug detail — {sum(len(r['bugs']) for r in runs_with_bugs)} unique keys"):
            for r in runs_with_bugs:
                st.markdown(f"**{r['name']}** — {len(r['bugs'])} bug(s)")
                st.code(", ".join(r["bugs"]), language=None)


def _render_stability(bu: str, project_ids: set[int]) -> None:
    st.markdown("#### 📈 Test Stability")
    st.caption(
        "Classify cases by their result pattern across the last N **completed** runs. "
        "*Always fail* → fix priority · *Flaky* → investigate · *Always pass* → safe · "
        "*Insufficient data* → < 1 execution recorded."
    )

    c1, _ = st.columns([1, 3])
    n_runs = c1.selectbox("Runs to analyse", [3, 5, 10, 20], index=1,
                          key=f"stab_n_{bu}")

    with st.spinner("Fetching completed runs + their tests…"):
        runs    = _completed_runs_for_bu(bu, project_ids, limit=n_runs)
        if not runs:
            st.info("No completed runs found for this BU.")
            return
        stab = _classify_stability(runs)

    if stab.empty:
        st.info("No test data found in the selected runs.")
        return

    # Summary chips: counts per classification
    counts = stab["classification"].value_counts().to_dict()
    chips = st.columns(len(_CLASS_ORDER))
    for col, cls in zip(chips, _CLASS_ORDER):
        col.metric(f"{_CLASS_EMOJI[cls]} {cls}", f"{counts.get(cls, 0):,}")

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

    st.dataframe(
        sub[["case_id", "title", "pattern", "executions", "pass", "fail",
             "blocked", "classification", "failure_rate"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "case_id":        st.column_config.NumberColumn("ID", width="small"),
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
