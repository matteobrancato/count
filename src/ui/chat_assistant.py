"""Floating AI chat assistant — Gemini-powered Q&A over TestRail data.

A pill-shaped floating button at the bottom-left of every page opens a
popover-style chat panel (Rovo-like UX) where managers and QA leads can ask
natural-language questions such as "How is Superdrug doing?" or "What are the
top failing tests in Drogas?".

Architecture
────────────
We rely on Gemini's automatic function calling: the LLM never sees raw
TestRail dumps — it calls our Python tool functions, gets exact numbers
back, and formulates the answer.  This eliminates hallucinations on metrics.

Tools exposed to Gemini
───────────────────────
    list_bus()                 — list of all Business Units in the dashboard
    get_bu_coverage(bu)        — coverage % + top areas + regression baseline
    get_active_runs(bu)        — open runs with pass/fail/completion
    get_open_bugs(bu)          — unique JIRA keys with the test that generated them
    get_test_stability(bu, …)  — always-pass / always-fail / flaky counts + top
    compare_bus()              — ranking of all BUs by coverage %

Privacy
───────
Only the user question + tool results travel to the Gemini API.  Raw test-case
content and PII never leave the app.

Setup
─────
Add `GEMINI_API_KEY` to `.streamlit/secrets.toml`.  Free key:
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


# ── Lazy Gemini import — the app must boot even without the dep installed ────
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
- Use full Business Unit names in your replies (e.g. "Superdrug", not "SD").
- Format big numbers with commas (e.g. "2,032").
- Always include context (e.g. "out of 7,234 total cases").
- For "how are we doing" questions: lead with the headline coverage %, then
  1-2 lines of context, then call out the biggest gap if relevant.
- If a question is ambiguous, ask one clarifying question instead of guessing.
- If the user asks about a BU you don't recognise, call list_bus() first.
- For comparisons across BUs, use compare_bus().
"""

# Suggestion chips shown in the empty-state of the chat panel.
# Pairs: (display label, full question sent to Gemini).
_SUGGESTIONS: list[tuple[str, str]] = [
    ("📊 How is Superdrug doing?",    "How is Superdrug doing on automation?"),
    ("🏆 Compare all BUs",            "Compare all BUs by coverage and tell me who is ahead."),
    ("🐛 Open bugs in Watsons",       "What bugs are currently open for Watsons?"),
    ("🌀 Flaky tests in Drogas",      "Which tests are flaky for Drogas? Top offenders."),
]


# ── BU resolution ────────────────────────────────────────────────────────────
def _resolve_bu_name(query: str) -> str | None:
    """Map a user-supplied BU code/name to the canonical display name."""
    if not query:
        return None
    q   = query.lower().strip()
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

    Useful to answer "What bugs are open for Drogas?" — returns one record per
    failure event with the test ID/title, run name, and date.

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
        except Exception as exc:                                        # noqa: BLE001
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


@st.cache_resource(show_spinner=False)
def _get_gemini_client(api_key: str):
    """One Gemini Client per app process — its underlying HTTP pool is shared.

    Using `@st.cache_resource` instead of `st.session_state` avoids the
    "Cannot send a request, as the client has been closed" error: Streamlit
    serialises session_state values on each rerun, which closes the client's
    httpx pool.  `cache_resource` is the documented escape hatch for stateful
    objects that must outlive a rerun.
    """
    return genai.Client(api_key=api_key)


def _gemini_ready() -> bool:
    return _GEMINI_AVAILABLE and _get_api_key() is not None


def _clear_chat() -> None:
    st.session_state.pop("ai_chat_messages", None)


def _send_message(text: str) -> None:
    """Send *text* to Gemini, appending both turns to the conversation log.

    Uses `client.models.generate_content` with the full conversation history as
    `contents` (no persistent Chat object) — robust against Streamlit reruns.
    Function calling is auto-handled by the SDK: tool calls happen server-side
    in a single round-trip, only the final text response comes back to us.
    """
    if not _GEMINI_AVAILABLE:
        return
    api_key = _get_api_key()
    if not api_key:
        return

    msgs = st.session_state.setdefault("ai_chat_messages", [])
    msgs.append({"role": "user", "content": text})

    # Build Gemini-format conversation history from our plain message log.
    contents = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part(text=m["content"])],
        )
        for m in msgs
    ]

    config = types.GenerateContentConfig(
        tools=_TOOLS,
        system_instruction=_SYSTEM_INSTRUCTION.strip(),
    )

    try:
        client = _get_gemini_client(api_key)
        response = client.models.generate_content(
            model=_MODEL, contents=contents, config=config,
        )
        reply = (response.text or "").strip() or "(empty response)"
    except Exception as exc:                                            # noqa: BLE001
        reply = f"⚠️ Error from Gemini: `{exc}`"
        logger.exception("Gemini chat call failed")

    msgs.append({"role": "assistant", "content": reply})


# ── UI ───────────────────────────────────────────────────────────────────────
# Streamlit ≥1.39 adds the class `st-key-{key}` on any element with a custom key.
# We anchor our CSS on that — far more robust than `:has()` tricks.
_FAB_CSS = """
<style>
/* ── 1. Pin the whole assistant block fixed bottom-left of the viewport ── */
.st-key-ai_assistant_fab {
    position: fixed !important;
    bottom: 24px !important;
    left:   24px !important;
    z-index: 9999 !important;
    width: auto !important;
    max-width: none !important;
    margin: 0 !important;
    padding: 0 !important;
}

/* ── 2. Style the popover trigger to look like a colourful pill FAB ───── */
.st-key-ai_assistant_fab [data-testid="stPopover"] button,
.st-key-ai_assistant_fab button[data-testid="stBaseButton-secondary"],
.st-key-ai_assistant_fab .stPopover > div > button,
.st-key-ai_assistant_fab button {
    border-radius: 28px !important;
    height: 56px !important;
    padding: 0 22px !important;
    font-size: 15px !important;
    font-weight: 600 !important;
    background: linear-gradient(135deg, #ED7D31 0%, #C00000 100%) !important;
    color: #fff !important;
    border: none !important;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18) !important;
    transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
}

.st-key-ai_assistant_fab button:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.25) !important;
    background: linear-gradient(135deg, #F08F4A 0%, #D11A1A 100%) !important;
}

.st-key-ai_assistant_fab button:active {
    transform: translateY(0);
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.18) !important;
}

/* ── 3. Size the popover panel that opens above the FAB ───────────────── */
div[data-baseweb="popover"] {
    /* Streamlit's popover content lives inside this baseweb wrapper */
    min-width: 380px;
}
.st-key-ai_assistant_fab [data-testid="stPopoverBody"] {
    min-width: 380px;
    max-width: min(480px, 92vw);
    max-height: min(640px, 75vh);
    overflow-y: auto;
}
</style>
"""


def _render_chat_panel() -> None:
    """The content of the popover — the actual chat UI."""
    # ── prerequisites ─────────────────────────────────────────────────────
    if not _GEMINI_AVAILABLE:
        st.error(
            "`google-genai` not installed.  Add it to `requirements.txt` and "
            "reboot the Streamlit Cloud app (Manage app → Reboot)."
        )
        return
    if not _get_api_key():
        st.error(
            "**`GEMINI_API_KEY`** missing from `secrets`. "
            "Get a free key at [aistudio.google.com/apikey]"
            "(https://aistudio.google.com/apikey)."
        )
        return

    st.markdown("### ✨ AI Assistant")
    st.caption(
        "Ask anything about automation coverage, runs, bugs, or test stability. "
        "Numbers come live from TestRail — no made-up data."
    )

    msgs = st.session_state.get("ai_chat_messages", [])

    # ── empty-state suggestion chips ──────────────────────────────────────
    if not msgs:
        st.markdown("**Try asking:**")
        cols = st.columns(2)
        for i, (label, question) in enumerate(_SUGGESTIONS):
            if cols[i % 2].button(label, key=f"ai_sugg_{i}",
                                  use_container_width=True):
                _send_message(question)
                st.rerun()
        st.markdown("---")

    # ── conversation history ──────────────────────────────────────────────
    for msg in msgs:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── input ─────────────────────────────────────────────────────────────
    user_input = st.chat_input("Ask anything…")
    if user_input:
        with st.spinner("Thinking…"):
            _send_message(user_input)
        st.rerun()

    # ── footer ────────────────────────────────────────────────────────────
    c1, c2 = st.columns([1, 3])
    if c1.button("🗑 Clear", key="ai_chat_clear", use_container_width=True):
        _clear_chat()
        st.rerun()
    c2.caption(f"✨ {_MODEL} · Uses AI · {len(msgs)} message(s)")


def render_floating_button() -> None:
    """Render the floating chat trigger at the bottom-left of the page.

    Uses a keyed container so we can position it fixed via the
    `.st-key-ai_assistant_fab` CSS class.  Idempotent — safe to call once per
    page render.  Even without an API key the button still appears (the
    missing-key message shows up inside the popover).
    """
    st.markdown(_FAB_CSS, unsafe_allow_html=True)

    # Keyed container = CSS hook for fixed positioning (Streamlit ≥1.39).
    with st.container(key="ai_assistant_fab"):
        # The popover trigger button IS the FAB.  Click → panel opens above it.
        with st.popover("💬 Ask Dexter", use_container_width=False,
                        help="Ask Dexter anything about Automation"):
            _render_chat_panel()
