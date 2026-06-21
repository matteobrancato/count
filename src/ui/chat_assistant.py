"""Floating AI chat assistant — Gemini-powered Q&A over TestRail data.

A pill-shaped floating button at the bottom-left of every page opens a
popover-style chat panel (Rovo-like UX) where managers and QA leads can ask
natural-language questions such as "How is Superdrug doing?" or "What are the
top failing tests in Drogas?".

Architecture
────────────
Reliability-first, two-layer design so we stay inside the free Gemini tier:

  1. A compact "LIVE COVERAGE SNAPSHOT" of EVERY BU (coverage %, regression
     baseline, production sanity, weakest areas, ranking) is pre-built from the
     same cached rule-evaluation the dashboard uses and injected into the system
     instruction.  Coverage / comparison / gap questions are therefore answered
     from context in a SINGLE API call — no multi-hop function calling.

  2. A small set of on-demand tools covers only the live detail that is too
     heavy to pre-compute for every BU.  The model calls at most one, and only
     when the question clearly needs it.

This keeps metrics exact (no hallucinated numbers) while cutting API calls ~5×
versus pure function calling — the previous design burned the daily quota fast.

On-demand tools exposed to Gemini
─────────────────────────────────
    get_active_runs(bu)        — open runs with pass/fail/completion
    get_open_bugs(bu)          — unique JIRA keys with the test that generated them
    get_test_stability(bu, …)  — always-pass / always-fail / flaky counts + top

Internal helpers (NOT exposed — used to build the snapshot)
──────────────────────────────────────────────────────────
    list_bus() · get_bu_coverage(bu) · compare_bus()

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
import re
import time
from typing import Any

import streamlit as st

from .. import testrail_client as tr
from ..bu_rules import ALL_RULES, BU_RUN_ALIASES
from ..rules_engine import evaluate_rules
from . import coverage_tab, runs_tab
from .styles import COLORS

logger = logging.getLogger(__name__)


# ── Lazy Gemini import — the app must boot even without the dep installed ────
try:
    from google import genai
    from google.genai import types
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False


_DEFAULT_MODEL = "gemini-2.5-flash"

# When `GEMINI_MODEL` is NOT explicitly set in secrets, we walk this chain on
# each request — picking the first model that's not currently rate-limited.
# Ordered preferred → most-likely-available.  The first one to reply wins.
_FALLBACK_CHAIN: list[str] = [
    "gemini-2.5-flash",       # best quality on free tier (10 RPM · 250 RPD)
    "gemini-2.5-flash-lite",  # higher free quota — great resilience (15 RPM · 1000 RPD)
    "gemini-2.0-flash",       # older, sometimes spare quota (15 RPM · 200 RPD)
]


def _configured_model() -> str | None:
    """Return the model from secrets if set, else None (= use fallback chain)."""
    try:
        v = st.secrets.get("GEMINI_MODEL")
        return v if v else None
    except Exception:                                                   # noqa: BLE001
        return None


def _models_to_try() -> list[str]:
    """Models to attempt in order for the current message.

    - If `GEMINI_MODEL` is set in secrets → use ONLY that (strict, no fallback).
    - Otherwise → walk the fallback chain.
    """
    configured = _configured_model()
    if configured:
        return [configured]
    return list(_FALLBACK_CHAIN)


def _display_model() -> str:
    """Model name shown in the footer caption."""
    used = st.session_state.get("ai_last_used_model")
    if used:
        return used
    return _configured_model() or _FALLBACK_CHAIN[0]


_RETRY_DELAY_RE = re.compile(
    r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s",
    re.IGNORECASE,
)


def _parse_retry_delay(err_str: str, default: float = 60.0) -> float:
    """Pull a `retryDelay: 16s` value out of a Gemini RESOURCE_EXHAUSTED error.

    The SDK exposes the original Google API error payload as the exception
    string, so a regex over the string body is the easiest way to grab the
    `RetryInfo` hint without depending on private SDK internals.
    """
    m = _RETRY_DELAY_RE.search(err_str)
    return float(m.group(1)) if m else default

_SYSTEM_INSTRUCTION = """
You are Dexter, the automation-coverage assistant for AS Watson's testing
platform.  You help managers and QA leads understand the state of test
automation across Business Units (BUs), and you can hold a real conversation
about it — follow-ups, comparisons, "why", "and the others?", etc.

The VALID Business Units are EXACTLY the ones listed in the "LIVE COVERAGE
SNAPSHOT" below — use those exact names and do NOT invent any others.  Note that
"Superdrug / Savers" is a real, separate entry (the suite of tests shared between
Superdrug and Savers); it is distinct from "Superdrug" and from "Savers".
Common aliases to map: SD=Superdrug, KV=Kruidvat, WTR=Watsons,
TPS=The Perfume Shop, ICI=ICI Paris XL, MRN=Marionnaud, DRO=Drogas.

# HOW TO ANSWER — read this carefully
A "LIVE COVERAGE SNAPSHOT" is provided below with the CURRENT numbers for ALL
BUs (overall coverage, No-Regression baseline, Production Sanity, weakest areas,
and the full ranking).  These numbers are live from TestRail.

  • For ANYTHING about coverage, totals, automated counts, comparisons,
    rankings, or coverage gaps → answer DIRECTLY from the snapshot.  Do NOT call
    any tool — the data is already in front of you.  This makes you fast and
    reliable.
  • Call a tool ONLY for live detail that is NOT in the snapshot:
      - get_active_runs(bu)    → currently open/running test runs + pass rates
      - get_open_bugs(bu)      → open JIRA bugs and the tests that raised them
      - get_test_stability(bu) → flaky / always-fail analysis over recent runs
    Call at most one tool, only when the question clearly needs that live detail.

Rules
─────
1. NEVER invent or estimate a number.  Use the snapshot, or a tool — nothing else.
2. **Reply in the user's language** (Italian → Italian, English → English) and
   match their tone.
3. Be concise and conversational.  Lead with the headline number in **bold**,
   then 1-3 short bullets.  Skip preamble like "Here is the data:".
4. Use full BU names ("Superdrug", not "SD").
5. Format numbers with thousands separators ("2,032", not "2032").
6. Always give context ("1,116 of 3,949 cases", "28.3% covered").
7. Be proactive in conversation: when it helps, add a one-line comparison or
   call out the weakest area — the snapshot has every BU, so use it.

CRITICAL — DO NOT ask for clarification when the user names a BU that is in the
snapshot (directly or via an alias).  "How is Superdrug doing?" is unambiguous →
answer from the snapshot immediately.  Only ask to clarify when no BU is
identifiable at all and the question needs one.

If a tool returns `{"error": "..."}`, tell the user the actual error — do not
make something up.

Answer shape (example for "how is X doing")
───────────────────────────────────────────
  **28.3%** automation coverage
  • 1,116 automated cases out of 3,949 total
  • No-Regression baseline: 92% covered (X/Y) · Production Sanity: 64% (X/Y)
  • Weakest area: <area> at 11%
"""

# Suggestion chips shown in the empty-state of the chat panel.
# Pairs: (display label, full question sent to Gemini).
_SUGGESTIONS: list[tuple[str, str]] = [
    ("📊  Superdrug status",   "How is Superdrug doing on automation?"),
    ("🏆  Compare all BUs",    "Compare all BUs by coverage and tell me who is ahead."),
    ("🐛  Bugs · Watsons",     "What bugs are currently open for Watsons?"),
    ("🌀  Flaky · Drogas",     "Which tests are flaky for Drogas? Top offenders."),
]


# ── BU resolution ────────────────────────────────────────────────────────────
def _safe_tool(fn):
    """Decorator: catch any exception in a tool function and return a dict the
    LLM can read, AND expose a signature with *resolved* type annotations.

    The resolved signature is critical for Gemini's automatic function calling.
    This module uses ``from __future__ import annotations`` (PEP 563), so a
    function's parameter annotations are stored as STRINGS ("str", "int").  The
    SDK's argument converter calls ``inspect.signature(fn)`` and then runs
    ``isinstance(value, param.annotation)`` — with a string annotation that
    raises *"isinstance() arg 2 must be a type"*, crashing every tool the model
    calls WITH arguments (parameterless tools slipped through).  By setting
    ``__signature__`` to a version whose annotations are the real types
    (via ``get_type_hints``), ``inspect.signature`` returns ``str``/``int`` and
    the SDK's isinstance check works.
    """
    import functools
    import inspect
    import typing

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                                        # noqa: BLE001
            logger.exception("Tool %s failed", fn.__name__)
            return {
                "error":         f"{type(exc).__name__}: {str(exc)[:200]}",
                "tool":          fn.__name__,
                "tool_arguments": {"args": list(args), "kwargs": dict(kwargs)},
            }

    # Rebuild the signature with resolved (real-type) annotations.
    try:
        hints = typing.get_type_hints(fn)
        sig = inspect.signature(fn)
        params = [
            p.replace(annotation=hints.get(name, p.annotation))
            for name, p in sig.parameters.items()
        ]
        wrapper.__signature__ = sig.replace(
            parameters=params,
            return_annotation=hints.get("return", sig.return_annotation),
        )
    except Exception:                                                   # noqa: BLE001
        pass  # fall back to the copied annotations if resolution fails
    return wrapper


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
@_safe_tool
def list_bus() -> dict:
    """List all Business Units available in the dashboard."""
    return {"business_units": sorted({r.bu for r in ALL_RULES})}


@_safe_tool
def get_bu_coverage(bu: str) -> dict:
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

    nd_ps, ab_ps, ids_ps = coverage_tab._filter_to_prod_sanity(non_dep, auto_bu)
    prod_sanity: dict[str, Any] = {}
    if not nd_ps.empty:
        ps_total = int(nd_ps["case_id"].nunique())
        ps_auto  = int(nd_ps["case_id"].isin(ids_ps).sum())
        prod_sanity = {
            "total_cases":      ps_total,
            "automated_unique": ps_auto,
            "coverage_pct":     round((ps_auto / ps_total * 100) if ps_total else 0.0, 1),
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
        "production_sanity":                 prod_sanity,
    }


@_safe_tool
def get_active_runs(bu: str) -> dict:
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


@_safe_tool
def get_open_bugs(bu: str) -> dict:
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


@_safe_tool
def get_test_stability(bu: str, n_runs: int = 5, min_executions: int = 5) -> dict:
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


@_safe_tool
def compare_bus() -> dict:
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


# Tools exposed to Gemini = ONLY the live/heavy detail that is NOT in the
# pre-built snapshot.  Coverage, totals, comparisons and gaps are answered
# directly from the snapshot (see `_build_coverage_brief`) in a single API call —
# this is the core of the reliability fix (no multi-hop function calling for the
# common case, so we stay well within the free-tier rate limits).
_TOOLS = [get_active_runs, get_open_bugs, get_test_stability]


@st.cache_data(ttl=3600, show_spinner=False)
def _build_coverage_brief() -> str:
    """Build a compact markdown snapshot of CURRENT coverage for every BU.

    Injected into the system instruction so Gemini can answer coverage,
    comparison and gap questions from context in a SINGLE call — no tool
    round-trips.  Cheap to build: it reuses `get_bu_coverage`, which is backed by
    the same `@st.cache_data` rule-evaluation the dashboard already uses.  Cached
    here too (and cleared by the header's "Refresh Numbers" button).
    """
    bus = sorted({r.bu for r in ALL_RULES})
    ranking: list[tuple[str, float]] = []
    blocks: list[str] = []

    for bu in bus:
        d = get_bu_coverage(bu)
        if not isinstance(d, dict) or "error" in d:
            blocks.append(f"## {bu}\n- (no data available right now)")
            continue
        ranking.append((d["business_unit"], d["coverage_pct"]))
        lines = [
            f"## {d['business_unit']}",
            f"- Overall coverage: {d['coverage_pct']}% "
            f"({d['automated_unique']:,} automated of {d['total_cases']:,} cases)",
        ]
        rb = d.get("regression_baseline") or {}
        if rb:
            lines.append(
                f"- No-Regression baseline: {rb['coverage_pct']}% "
                f"({rb['automated_unique']:,}/{rb['total_cases']:,})"
            )
        ps = d.get("production_sanity") or {}
        if ps:
            lines.append(
                f"- Production Sanity: {ps['coverage_pct']}% "
                f"({ps['automated_unique']:,}/{ps['total_cases']:,})"
            )
        areas = d.get("top_areas") or []
        weak = sorted(
            (a for a in areas if a.get("total", 0) >= 10),
            key=lambda a: a.get("coverage_pct", 0.0),
        )[:5]
        if weak:
            lines.append("- Weakest areas (lowest coverage first):")
            for a in weak:
                lines.append(
                    f"    - {a['area']}: {a['coverage_pct']}% "
                    f"({a['automated_rows']}/{a['total']})"
                )
        blocks.append("\n".join(lines))

    ranking.sort(key=lambda x: -x[1])
    rank_line = "Coverage ranking (high → low): " + " > ".join(
        f"{bu} {pct}%" for bu, pct in ranking
    )
    header = (
        "These are the CURRENT automation-coverage numbers, live from TestRail. "
        "Use them directly to answer coverage / comparison / gap questions.\n"
    )
    return f"{header}\n{rank_line}\n\n" + "\n\n".join(blocks)


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


def _queue_user_message(text: str) -> None:
    """Append the user's message so the next rerun can show it + generate a reply.

    Splitting "queue" from "generate" is what makes the UI clean: a chip click
    or form submit only queues + reruns (instant — the empty-state chips vanish
    because the conversation is no longer empty), and the slow Gemini call then
    runs on the FOLLOWING render with a spinner.  Without this split, the call
    blocked while the chips were still on screen, greying the siblings.
    """
    st.session_state.setdefault("ai_chat_messages", []).append(
        {"role": "user", "content": text}
    )


def _generate_pending_response() -> None:
    """Generate Gemini's reply for the trailing (unanswered) user message.

    Tries each model in `_models_to_try()` in order, falling back to the next
    one if the current model is rate-limited (RESOURCE_EXHAUSTED / 429) or not
    found (404).  A short cooldown is recorded per-model so we don't keep
    hitting an exhausted one within the same session.

    Function calling is auto-handled by the SDK: tool calls happen server-side
    in a single round-trip, only the final text response comes back to us.
    """
    if not _GEMINI_AVAILABLE:
        return
    api_key = _get_api_key()
    if not api_key:
        return

    msgs = st.session_state.get("ai_chat_messages", [])
    if not msgs or msgs[-1]["role"] != "user":
        return  # nothing pending

    contents = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part(text=m["content"])],
        )
        for m in msgs
    ]

    # Inject the live coverage snapshot into the system instruction so the model
    # answers coverage / comparison / gap questions from context in ONE call.
    try:
        brief = _build_coverage_brief()
    except Exception:                                                   # noqa: BLE001
        logger.exception("Failed to build coverage brief")
        brief = ""
    system_instruction = _SYSTEM_INSTRUCTION.strip()
    if brief:
        system_instruction += "\n\n# LIVE COVERAGE SNAPSHOT\n" + brief

    config = types.GenerateContentConfig(
        tools=_TOOLS,
        system_instruction=system_instruction,
        # The common case (coverage / comparison / gaps) needs ZERO tool calls —
        # it's answered from the snapshot above.  A small budget remains for the
        # occasional live-detail question (runs / bugs / stability): one tool
        # call + the formatting turn.  Keeping this low is what holds us inside
        # the free-tier rate limits.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=4,
        ),
        # Low temperature: this is a factual data assistant, not a creative one.
        temperature=0.3,
    )

    candidates = _models_to_try()
    cooling: dict[str, float] = st.session_state.setdefault("ai_exhausted_models", {})
    now = time.time()

    reply: str | None = None
    used_model: str | None = None
    last_err: str = ""

    for model in candidates:
        # Skip models still in cooldown (rate-limit or 404 hit recently).
        if cooling.get(model, 0.0) > now:
            continue
        try:
            client = _get_gemini_client(api_key)
            response = client.models.generate_content(
                model=model, contents=contents, config=config,
            )
            reply = (response.text or "").strip() or "(empty response)"
            used_model = model
            break
        except Exception as exc:                                        # noqa: BLE001
            err_str = str(exc)
            last_err = err_str
            if "limit: 0" in err_str:
                # Account-level (no free tier on this model) — long cooldown:
                # nothing we can do server-side, retry tomorrow.
                cooling[model] = now + 24 * 3600
                logger.info("Model %s has no quota (limit: 0) — trying next", model)
                continue
            if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
                # Transient rate limit — honour Gemini's suggested retryDelay.
                cooling[model] = now + _parse_retry_delay(err_str, default=60.0)
                logger.info("Model %s rate-limited — trying next", model)
                continue
            if ("503" in err_str or "UNAVAILABLE" in err_str
                    or "overload" in err_str.lower()):
                # Server-side overload (e.g. Gemini's flash model under heavy
                # demand).  Transient — short cooldown, fallback to next model.
                cooling[model] = now + 30.0
                logger.info("Model %s overloaded (503) — trying next", model)
                continue
            if "404" in err_str or "NOT_FOUND" in err_str:
                # Model doesn't exist — never retry within this session.
                cooling[model] = now + 9_999_999
                logger.info("Model %s not found — trying next", model)
                continue
            # Different error → don't waste fallbacks, break out.
            logger.exception("Unexpected Gemini error from %s", model)
            break

    if reply is None:
        # Every candidate failed.  Compose the most useful message we can.
        if "limit: 0" in last_err:
            reply = (
                "⚠️ **Your Google AI Studio account has no free-tier quota** "
                "for the available models (`limit: 0` on every fallback).  "
                "Common for new EU/UK accounts: the free tier exists, but "
                "needs billing enabled on the Google Cloud project to be "
                "unlocked.  Linking a card does **NOT** charge you under "
                "the free tier — it just unlocks the quota.\n\n"
                "Fix: [console.cloud.google.com/billing]"
                "(https://console.cloud.google.com/billing)."
            )
        elif "RESOURCE_EXHAUSTED" in last_err or "429" in last_err:
            reply = (
                "⚠️ **All fallback models hit their rate limit.**  Wait "
                "a minute and try again — RPM resets every 60 seconds, "
                "RPD resets at midnight UTC."
            )
        elif ("503" in last_err or "UNAVAILABLE" in last_err
              or "overload" in last_err.lower()):
            reply = (
                "⚠️ **Gemini is temporarily overloaded.**  All fallback "
                "models reported high demand on the free tier.  Wait "
                "30-60 seconds and try again — this is server-side and "
                "usually clears in under a minute."
            )
        elif "404" in last_err or "NOT_FOUND" in last_err:
            reply = (
                "⚠️ **No usable Gemini model found.**  Set `GEMINI_MODEL` "
                "in `secrets.toml` to a valid one (e.g. `gemini-2.5-flash`)."
            )
        else:
            short = (last_err or "no error captured").split("\n", 1)[0][:240]
            reply = f"⚠️ Error from Gemini: `{short}`"

    st.session_state["ai_last_used_model"] = used_model
    msgs.append({"role": "assistant", "content": reply})


# ── UI ───────────────────────────────────────────────────────────────────────
# Streamlit ≥1.39 adds the class `st-key-{key}` on any element with a custom key.
# We anchor our CSS on that — far more robust than `:has()` tricks.
_FAB_CSS = """
<style>
/* ── 1. The keyed container IS the FAB — a fixed, ALWAYS-EXPANDED chat pill.
       No animation, no morph.  A modern SVG sparkle icon (::before) + the
       "Ask Dexter" text are centred as one group.  Nothing can clip or drift. */
.st-key-ai_assistant_fab {
    position: fixed !important;
    bottom: 24px !important;
    left:   24px !important;
    z-index: 9999 !important;
    width: 158px !important;
    height: 48px !important;
    margin: 0 !important;
    padding: 0 !important;
}

/* ── 2. Every inner wrapper fills the pill ─── */
.st-key-ai_assistant_fab [data-testid="stPopover"],
.st-key-ai_assistant_fab .stPopover,
.st-key-ai_assistant_fab [data-testid="stPopover"] > div,
.st-key-ai_assistant_fab .stPopover > div {
    width: 100% !important;
    height: 100% !important;
    min-width: 0 !important;
    max-width: none !important;
    margin: 0 !important;
    padding: 0 !important;
}

/* Hide Streamlit's popover chevron/caret icon — only the icon + label show. */
.st-key-ai_assistant_fab button [data-testid="stIconMaterial"],
.st-key-ai_assistant_fab button svg {
    display: none !important;
}

/* ── 3. The button: a fixed-width pill, content perfectly centred. ───────── */
.st-key-ai_assistant_fab button {
    width: 100% !important;
    min-width: 100% !important;
    max-width: 100% !important;
    height: 48px !important;
    padding: 0 16px !important;
    border-radius: 24px !important;
    overflow: hidden !important;
    white-space: nowrap !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;   /* [icon + label] group centred */
    gap: 9px !important;                   /* space between icon and label */
    font-size: 15px !important;
    font-weight: 600 !important;
    line-height: 1 !important;
    background: #FF4B4B !important;
    color: #fff !important;
    border: none !important;
    box-shadow: 0 4px 14px rgba(255, 75, 75, 0.42) !important;
    transition: box-shadow 0.16s ease, background 0.16s ease !important;
}

/* Modern SVG sparkle icon, drawn as a ::before flex item so it sits LEFT of the
   label and the pair centres together.  White, fixed 17px square. */
.st-key-ai_assistant_fab button::before {
    content: "" !important;
    flex: 0 0 auto !important;
    width: 17px !important;
    height: 17px !important;
    background: url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20fill='%23ffffff'%3E%3Cpath%20d='M12%202c.4%203.7%201%205.3%202.4%206.7C15.8%2010.1%2017.4%2010.7%2021%2011c-3.6.4-5.2%201-6.6%202.4C13%2014.8%2012.4%2016.4%2012%2020c-.4-3.6-1-5.2-2.4-6.6C8.2%2012%206.6%2011.4%203%2011c3.6-.3%205.2-.9%206.6-2.3C11%207.3%2011.6%205.7%2012%202z'/%3E%3Cpath%20d='M19%203c.15%201.2.4%201.7.85%202.15.45.45.95.7%202.15.85-1.2.15-1.7.4-2.15.85-.45.45-.7.95-.85%202.15-.15-1.2-.4-1.7-.85-2.15C17.7%205.55%2017.2%205.3%2016%205.15c1.2-.15%201.7-.4%202.15-.85C18.6%203.85%2018.85%203.35%2019%203z'/%3E%3C/svg%3E") no-repeat center / contain !important;
}

/* Hover/active only change colour + shadow — never size or position. */
.st-key-ai_assistant_fab button:hover {
    background: #E63E3E !important;
    box-shadow: 0 6px 22px rgba(255, 75, 75, 0.55) !important;
}
.st-key-ai_assistant_fab button:active {
    background: #D63030 !important;
    box-shadow: 0 2px 8px rgba(255, 75, 75, 0.35) !important;
}

/* The "Ask Dexter" label: a natural-width flex item (NOT grow) so the
   [icon + label] pair centres as a group.  Force WHITE text (global markdown
   rules would otherwise tint it dark slate on the red pill). */
/* Any wrapper between the button and the label must NOT grow, or it would fill
   the pill and left-align the text (the decentering bug).  Descendant selector
   (not `>`) so it applies however deep Streamlit nests the markdown. */
.st-key-ai_assistant_fab button > div,
.st-key-ai_assistant_fab button [data-testid="stMarkdownContainer"] {
    flex: 0 0 auto !important;
    width: auto !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}
.st-key-ai_assistant_fab button div,
.st-key-ai_assistant_fab button p,
.st-key-ai_assistant_fab button span,
.st-key-ai_assistant_fab button * {
    color: #fff !important;
    line-height: 1 !important;
    padding: 0 !important;
    margin: 0 !important;
    white-space: nowrap !important;
}

/* ── 3. Size the chat panel that opens above the FAB ──────────────────── */
/* Target ONLY Streamlit's popover body (st.popover content) — the chat panel
   is the app's only st.popover.  Selectbox/multiselect dropdowns use baseweb
   menu/listbox, NOT stPopoverBody, so they're untouched (previously a global
   popover min-width broke them into an oversized white box). */
[data-testid="stPopoverBody"] {
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

    msgs = st.session_state.get("ai_chat_messages", [])
    sid  = st.session_state.get("ai_chat_session_id", 0)

    # ── header: identity on the left, a quiet "Delete chat" link on the right.
    # The link only appears once there's a conversation to delete.  Deleting
    # bumps the session_id so widget keys change, forcing Streamlit to treat
    # every form/button as brand-new (avoids stale widget state leaking across
    # reruns inside the popover).
    head_l, head_r = st.columns([3, 2], vertical_alignment="center")
    head_l.markdown(
        f"<div style='font-size:19px;font-weight:800;color:{COLORS['ink']};"
        f"letter-spacing:-0.01em'>✨ Dexter</div>"
        f"<div style='font-size:11.5px;color:{COLORS['muted']};margin-top:1px'>"
        f"AI coverage assistant</div>",
        unsafe_allow_html=True,
    )
    if msgs:
        if head_r.button("🗑 Delete chat", key="ai_delete_chat",
                         use_container_width=True):
            st.session_state["ai_chat_messages"]   = []
            st.session_state["ai_chat_session_id"] = sid + 1
            st.rerun()

    st.caption(
        "Ask about automation coverage, runs, bugs, or test stability — "
        "numbers come live from TestRail, never made up."
    )

    # ── empty-state suggestion chips ──────────────────────────────────────
    # Clicking a chip only QUEUES the message and reruns — the chips vanish
    # immediately (conversation no longer empty), so no sibling ever greys out.
    if not msgs:
        st.markdown(
            f"<div style='font-size:12px;font-weight:700;color:{COLORS['muted']};"
            f"text-transform:uppercase;letter-spacing:0.06em;margin:2px 0 8px'>"
            f"Try asking</div>",
            unsafe_allow_html=True,
        )
        cols = st.columns(2)
        for i, (label, question) in enumerate(_SUGGESTIONS):
            if cols[i % 2].button(label, key=f"ai_sugg_{sid}_{i}",
                                  use_container_width=True):
                _queue_user_message(question)
                st.rerun()
        st.markdown("")

    # ── conversation history ──────────────────────────────────────────────
    for msg in msgs:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # ── generate the reply for a freshly-queued user message ──────────────
    if msgs and msgs[-1]["role"] == "user":
        n_before = len(msgs)
        with st.chat_message("assistant"), st.spinner("Thinking…"):
            _generate_pending_response()
        # Only rerun if a reply was actually appended — guards against an
        # infinite loop should generation ever return without a response.
        if len(st.session_state.get("ai_chat_messages", [])) > n_before:
            st.rerun()

    # ── input ─────────────────────────────────────────────────────────────
    # A form (instead of `st.chat_input`) lets us keep everything inside the
    # popover cleanly — `chat_input` has known double-render quirks when nested
    # in popovers because it tries to position itself fixed at the container
    # bottom.  The session_id in the key resets the form on each new chat.
    with st.form(key=f"ai_chat_form_{sid}", clear_on_submit=True, border=False):
        cols = st.columns([5, 1])
        user_input = cols[0].text_input(
            "Message", placeholder="Ask anything…",
            label_visibility="collapsed", key=f"ai_input_{sid}",
        )
        submitted = cols[1].form_submit_button("→", use_container_width=True)
    if submitted and user_input.strip():
        _queue_user_message(user_input.strip())
        st.rerun()

    # ── footer ────────────────────────────────────────────────────────────
    st.markdown(
        f"<div style='text-align:right;font-size:10.5px;color:{COLORS['muted']};"
        f"margin-top:6px'>{_display_model()} · Uses AI</div>",
        unsafe_allow_html=True,
    )


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
        # The popover trigger button IS the FAB — an always-expanded pill whose
        # label is plain "Ask Dexter"; the sparkle icon is CSS ::before.  No
        # animation, nothing to clip or drift.
        with st.popover("Ask Dexter", use_container_width=False):
            _render_chat_panel()
