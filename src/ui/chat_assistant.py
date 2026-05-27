"""Floating AI chat assistant — Gemini-powered Q&A over TestRail data.

A floating button at the bottom-left of every page opens a chat dialog where
managers and QA leads can ask natural-language questions like
"How is Superdrug doing?" or "What are the top failing tests in Drogas?".

Architecture
────────────
We rely on Gemini's automatic function calling: the LLM never sees raw
TestRail dumps — it calls our Python tool functions, gets exact numbers back,
and formulates the answer.  This eliminates hallucinations on metrics.

Tools exposed to Gemini
───────────────────────
    list_bus()                 — list of all Business Units in the dashboard
    get_bu_coverage(bu)        — coverage % + top areas + regression baseline
    get_active_runs(bu)        — open runs with pass/fail/completion
    get_open_bugs(bu)          — unique JIRA keys with the test that generated them
    get_test_stability(bu, …)  — always-pass / always-fail / flaky counts + top
    compare_bus()              — ranking of all BUs by coverage %

Privacy: only the user question + tool results travel to the Gemini API.  Raw
test-case content and PII never leave the app.

Setup: add `GEMINI_API_KEY` to `.streamlit/secrets.toml`.  Get a free key at
https://aistudio.google.com/apikey.
"""
from __future__ import annotations

import logging
from typing import Any

import streamlit as st

from .. import testrail_client as tr
from ..bu_rules import ALL_RULES, BU_RUN_ALIASES
from ..rules_engine import evaluate_rules
from . import coverage_tab, runs_tab

logger = logging.getLogger(__name__)


# ── Lazy Gemini import (app must boot even without the dep installed) ────────
try:
    from google import genai
    from google.genai import types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False


_MODEL = "gemini-2.0-flash"

_SYSTEM_INSTRUCTION = """
You are an automation coverage assistant for AS Watson's testing platform.
You help managers and QA leads understand the state of test automation across
Business Units (BUs).

Available BUs:
  Superdrug, Savers, The Perfume Shop, Kruidvat, Trekpleister, Watsons,
  ICI Paris XL, Marionnaud, Drogas, Next Gen.

Rules:
- ALWAYS use the provided tools to get exact numbers. Never invent or estimate.
- Be concise: managers want short answers (3-6 lines, bullets where helpful).
- Use full Business Unit names in your replies (e.g. "Superdrug", not "SD") —
  the tools accept both, but the reply should be readable.
- Format big numbers with commas (e.g. "2,032").
- Always include context (e.g. "out of 7,234 total cases").
- For "how are we doing" questions: lead with the headline coverage %, then
  1-2 lines of context, then call out the biggest gap if relevant.
- If a question is ambiguous, ask one clarifying question instead of guessing.
- If the user asks about a BU you don't recognise, call list_bus() first.
- For comparisons across BUs, use compare_bus().
"""


# ── BU resolution ────────────────────────────────────────────────────────────
def _resolve_bu_name(query: str) -> str | None:
    """Map a user-supplied BU code/name to the canonical display name."""
    if not query:
        return None
    q = query.lower().strip()
    bus = sorted({r.bu for r in ALL_RULES})

    for bu in bus:                                          # exact
        if bu.lower() == q:
            return bu
    for bu, aliases in BU_RUN_ALIASES.items():              # alias (SD → Superdrug)
        for alias in aliases:
            if alias.lower() == q:
                return bu
    for bu in bus:                                          # loose substring
        if q in bu.lower() or bu.lower() in q:
            return bu
    return None


# ── Tool functions exposed to Gemini ─────────────────────────────────────────
def list_bus() -> dict[str, Any]:
    """List all Business Units available in the dashboard."""
    return {"business_units": sorted({r.bu for r in ALL_RULES})}


def get_bu_coverage(bu: str) -> dict[str, Any]:
    """Get automation coverage for a Business Unit.

    Returns total non-deprecated cases, automated count, coverage percentage,
    plus the top 15 functional areas (TestRail sections) and the regression
    baseline coverage (cases tagged with big_regr_desktop / big_regr_mobile).

    Args:
        bu: BU name or alias (e.g. "Superdrug" or "SD").
    """
    canonical = _resolve_bu_name(bu)
    if not canonical:
        return {"error": f"Unknown BU '{bu}'. Call list_bus() to see options."}

    scope = next((r.scope for r in ALL_RULES if r.bu == canonical), "website")
    rules = [r for r in ALL_RULES if r.scope == scope]
    result = evaluate_rules(tuple(r.name for r in rules))
    raw, auto = result.raw_cases, result.automated

    bu_suites = {r.suite_id for r in rules if r.bu == canonical}
    raw_bu  = raw[raw["suite_id"].isin(bu_suites)] if not raw.empty else raw
    auto_bu = auto[auto["bu"] == canonical] if not auto.empty else auto

    if raw_bu.empty:
        return {"error": f"No data loaded for {canonical}"}

    non_dep  = raw_bu[raw_bu["deprecated"] == False]  # noqa: E712
    auto_ids = set(auto_bu["case_id"].unique()) if not auto_bu.empty else set()

    total       = int(non_dep["case_id"].nunique())
    auto_unique = int(non_dep["case_id"].isin(auto_ids).sum())
    cov_pct     = round((auto_unique / total * 100) if total else 0.0, 1)

    cov_table, _ = coverage_tab._coverage_table(non_dep, auto_bu, auto_ids, depth_offset=0)
    top_areas = []
    if not cov_table.empty:
        for _, row in cov_table.head(15).iterrows():
            top_areas.append({
                "area":           str(row["section"]),
                "total":          int(row["total"]),
                "automated_rows": int(row["automated"]),
                "coverage_pct":   float(row["coverage_pct"]),
            })

    nd_base, ab_base, ids_base = coverage_tab._filter_to_regression_baseline(non_dep, auto_bu)
    regression: dict[str, Any] = {}
    if not nd_base.empty:
        regr_total = int(nd_base["case_id"].nunique())
        regr_auto  = int(nd_base["case_id"].isin(ids_base).sum())
        regression = {
            "total_cases":      regr_total,
            "automated_unique": regr_auto,
            "coverage_pct":     round((regr_auto / regr_total * 100) if regr_total else 0.0, 1),
        }

    return {
        "business_unit":                     canonical,
        "scope":                             scope,
        "total_cases":                       total,
        "automated_unique":                  auto_unique,
        "automated_rows_desktop_plus_mobile": int(len(auto_bu)) if not auto_bu.empty else 0,
        "coverage_pct":                      cov_pct,
        "top_areas":                         top_areas,
        "regression_baseline":               regression,
    }


def get_active_runs(bu: str) -> dict[str, Any]:
    """Get the list of active (open) TestRail runs for a BU.

    Each run summary includes pass/fail/blocked counts, completion %,
    pass rate, days since last activity, and the unique open JIRA bugs.

    Args:
        bu: BU name or alias.
    """
    canonical = _resolve_bu_name(bu)
    if not canonical:
        return {"error": f"Unknown BU '{bu}'"}

    project_ids = runs_tab._bu_project_ids().get(canonical, set())
    if not project_ids:
        return {"error": f"No TestRail projects for {canonical}"}

    base_url    = tr.TestRailCredentials.from_secrets().base_url
    all_active  = runs_tab._flatten_active_runs(project_ids)
    bu_runs     = [
        r for r in all_active
        if canonical in runs_tab._bus_for_run_name(r.get("name"))
        or canonical in runs_tab._bus_for_run_name(r.get("plan_name"))
    ]
    rows = [runs_tab._summarise_run(r, base_url) for r in bu_runs]
    rows.sort(key=lambda r: -(r["updated_on"] or 0))

    bug_records = runs_tab._collect_bug_records(rows)
    bugs_by_run: dict[int, set[str]] = {}
    for rec in bug_records:
        bugs_by_run.setdefault(rec["run_id"], set()).add(rec["bug"])

    runs = []
    for r in rows:
        runs.append({
            "name":                r["name"],
            "plan":                r["plan"],
            "total_tests":         r["total"],
            "passed":              r["passed"],
            "failed":              r["failed"],
            "blocked":             r["blocked"],
            "completion_pct":      r["completion"],
            "pass_rate_pct":       r["pass_rate"],
            "days_since_activity": r["days_idle"],
            "last_activity":       r["updated_str"],
            "open_bugs":           sorted(bugs_by_run.get(r["id"], set())),
        })

    return {
        "business_unit":          canonical,
        "active_run_count":       len(rows),
        "unique_open_bugs_count": len({rec["bug"] for rec in bug_records}),
        "runs":                   runs,
    }


def get_open_bugs(bu: str) -> dict[str, Any]:
    """List the open JIRA bug keys for a BU, with the test that generated each.

    Useful to answer questions like "What bugs are open for Drogas?" — returns
    one record per failure event with the test ID/title, run name, and date.

    Args:
        bu: BU name or alias.
    """
    canonical = _resolve_bu_name(bu)
    if not canonical:
        return {"error": f"Unknown BU '{bu}'"}

    project_ids = runs_tab._bu_project_ids().get(canonical, set())
    if not project_ids:
        return {"error": f"No TestRail projects for {canonical}"}

    base_url   = tr.TestRailCredentials.from_secrets().base_url
    all_active = runs_tab._flatten_active_runs(project_ids)
    bu_runs    = [
        r for r in all_active
        if canonical in runs_tab._bus_for_run_name(r.get("name"))
        or canonical in runs_tab._bus_for_run_name(r.get("plan_name"))
    ]
    rows = [runs_tab._summarise_run(r, base_url) for r in bu_runs]
    bug_records = runs_tab._collect_bug_records(rows)

    return {
        "business_unit": canonical,
        "bug_count":     len({rec["bug"] for rec in bug_records}),
        "bugs": [
            {
                "key":         rec["bug"],
                "url":         rec["bug_url"],
                "test_id":     rec["case_id"],
                "test_title":  rec["case_title"],
                "run_name":    rec["run_name"],
                "failed_on":   rec["failed_str"],
            }
            for rec in bug_records
        ],
    }


def get_test_stability(bu: str, n_runs: int = 5, min_executions: int = 5) -> dict[str, Any]:
    """Analyse test stability over recent completed runs for a BU.

    Classifies each case as: Always pass, Always fail, Flaky, or Insufficient
    data, then returns counts plus the top 10 actionable cases (Always fail +
    Flaky, sorted by failure rate DESC).

    Args:
        bu:             BU name or alias.
        n_runs:         Most recent completed runs to walk (default 5).
        min_executions: Minimum results per case to receive a classification.
    """
    canonical = _resolve_bu_name(bu)
    if not canonical:
        return {"error": f"Unknown BU '{bu}'"}

    project_ids = runs_tab._bu_project_ids().get(canonical, set())
    if not project_ids:
        return {"error": f"No TestRail projects for {canonical}"}

    completed = runs_tab._completed_runs_for_bu(canonical, project_ids, limit=int(n_runs))
    if not completed:
        return {"business_unit": canonical, "error": "No completed runs found"}

    stab = runs_tab._classify_stability(completed, min_executions=int(min_executions))
    if stab.empty:
        return {"business_unit": canonical, "error": "No test data in selected runs"}

    counts = {str(k): int(v) for k, v in stab["classification"].value_counts().to_dict().items()}

    failing = stab[stab["classification"].isin(["Always fail", "Flaky"])]
    failing = failing.sort_values(["failure_rate", "fail"], ascending=[False, False]).head(10)
    top_failing = [
        {
            "case_id":        int(row["case_id"]),
            "title":          str(row["title"]),
            "classification": str(row["classification"]),
            "failure_rate":   float(row["failure_rate"]),
        }
        for _, row in failing.iterrows()
    ]

    return {
        "business_unit":         canonical,
        "runs_analyzed":         len(completed),
        "min_executions":        int(min_executions),
        "classification_counts": counts,
        "top_failing_cases":     top_failing,
    }


def compare_bus() -> dict[str, Any]:
    """Rank all Business Units by overall automation coverage %.

    Returns a list sorted from highest to lowest coverage, so the user can see
    at a glance who's ahead and who needs attention.
    """
    bus = sorted({r.bu for r in ALL_RULES})
    rankings: list[dict[str, Any]] = []
    for bu in bus:
        try:
            data = get_bu_coverage(bu)
        except Exception as exc:
            logger.warning("compare_bus: %s failed: %s", bu, exc)
            continue
        if "error" in data:
            continue
        rankings.append({
            "business_unit":    data["business_unit"],
            "coverage_pct":     data["coverage_pct"],
            "total_cases":      data["total_cases"],
            "automated_unique": data["automated_unique"],
        })
    rankings.sort(key=lambda r: -r["coverage_pct"])
    return {"ranking": rankings}


_TOOLS = [
    list_bus, get_bu_coverage, get_active_runs,
    get_open_bugs, get_test_stability, compare_bus,
]


# ── Gemini client / session ──────────────────────────────────────────────────
def _get_api_key() -> str | None:
    try:
        return st.secrets["GEMINI_API_KEY"]
    except (KeyError, FileNotFoundError):
        return None


def _get_chat_session():
    """Return the cached Gemini chat session, creating it on first call.

    Stored in `st.session_state` so the conversation history persists across
    Streamlit reruns (without it, every message would be a fresh chat).
    """
    if "ai_chat_session" in st.session_state:
        return st.session_state.ai_chat_session

    if not _GEMINI_AVAILABLE:
        return None
    api_key = _get_api_key()
    if not api_key:
        return None

    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        tools=_TOOLS,
        system_instruction=_SYSTEM_INSTRUCTION.strip(),
    )
    st.session_state.ai_chat_session  = client.chats.create(model=_MODEL, config=config)
    st.session_state.ai_chat_messages = []
    return st.session_state.ai_chat_session


def _clear_chat() -> None:
    st.session_state.pop("ai_chat_session",  None)
    st.session_state.pop("ai_chat_messages", None)


# ── UI ───────────────────────────────────────────────────────────────────────
# Floating Action Button CSS — anchored on a hidden marker so we can find the
# right element-container even though Streamlit's DOM is heavily nested.
_FAB_CSS = """
<style>
/* Hidden marker — pure CSS anchor */
#ai-fab-marker { display: none; }

/* The element-container that immediately follows the marker = our button */
div.element-container:has(#ai-fab-marker) + div.element-container {
    position: fixed;
    bottom: 24px;
    left: 24px;
    z-index: 9999;
    width: auto !important;
    margin: 0 !important;
    padding: 0 !important;
}

/* FAB pill button */
div.element-container:has(#ai-fab-marker) + div.element-container .stButton > button {
    border-radius: 28px;
    height: 56px;
    padding: 0 22px;
    font-size: 15px;
    font-weight: 600;
    background: linear-gradient(135deg, #ED7D31 0%, #C00000 100%);
    color: #fff !important;
    border: none;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
    transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}
div.element-container:has(#ai-fab-marker) + div.element-container .stButton > button:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.25);
    background: linear-gradient(135deg, #F08F4A 0%, #D11A1A 100%);
}
div.element-container:has(#ai-fab-marker) + div.element-container .stButton > button:active {
    transform: translateY(0);
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.18);
}
</style>
"""


@st.dialog("✨ AI Assistant", width="large")
def _chat_dialog() -> None:
    """The chat modal — opens when the FAB is clicked."""
    # ── prerequisites ─────────────────────────────────────────────────────
    if not _GEMINI_AVAILABLE:
        st.error(
            "The `google-genai` package isn't installed.  "
            "Add `google-genai` to `requirements.txt` and reboot the app."
        )
        return
    if not _get_api_key():
        st.error(
            "**`GEMINI_API_KEY`** is missing from `.streamlit/secrets.toml`.  "
            "Get a free key at "
            "[aistudio.google.com/apikey](https://aistudio.google.com/apikey) "
            "and add it to your secrets, then reboot the app."
        )
        return
    session = _get_chat_session()
    if session is None:
        st.error("Could not initialise the Gemini chat session.")
        return

    st.caption(
        "Ask anything about automation coverage, active runs, bugs, or test "
        "stability across all BUs.  Numbers come live from TestRail — no "
        "made-up data."
    )

    # ── conversation history ──────────────────────────────────────────────
    for msg in st.session_state.get("ai_chat_messages", []):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── input ─────────────────────────────────────────────────────────────
    user_input = st.chat_input("e.g. How is Superdrug doing?")
    if user_input:
        st.session_state.ai_chat_messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)
        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                try:
                    response = session.send_message(user_input)
                    reply = (response.text or "").strip() or "(empty response)"
                except Exception as exc:                                # noqa: BLE001
                    reply = f"⚠️ Error from Gemini: `{exc}`"
                    logger.exception("Gemini chat call failed")
            st.markdown(reply)
        st.session_state.ai_chat_messages.append({"role": "assistant", "content": reply})

    # ── footer ────────────────────────────────────────────────────────────
    c1, c2 = st.columns([1, 4])
    if c1.button("🗑 Clear chat", key="ai_chat_clear"):
        _clear_chat()
        st.rerun()
    c2.caption(
        f"Model: `{_MODEL}` · Powered by Google Gemini · "
        f"{len(st.session_state.get('ai_chat_messages', []))} message(s)"
    )


def render_floating_button() -> None:
    """Render the floating chat trigger at the bottom-left of the page.

    Idempotent (safe to call once per render).  Even without an API key the
    button still appears — the missing-key message shows up inside the dialog.
    """
    st.markdown(_FAB_CSS, unsafe_allow_html=True)
    # Hidden marker — anchors the CSS selector for the next element-container.
    st.markdown('<div id="ai-fab-marker"></div>', unsafe_allow_html=True)
    if st.button("💬 Ask AI", key="ai_fab_button",
                 help="Ask anything about coverage, runs and bugs"):
        _chat_dialog()
