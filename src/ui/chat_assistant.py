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
    "gemini-2.5-flash",   # solid free tier in most regions (10 RPM · 250 RPD)
    "gemini-2.0-flash",   # older, sometimes spare quota (15 RPM · 200 RPD)
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
You are an automation coverage assistant for AS Watson's testing platform.
You help managers and QA leads understand the state of test automation across
Business Units (BUs).

The complete list of valid Business Units is EXACTLY these 10 — there are NO
others, do NOT invent variants or combinations:

  - Superdrug
  - Savers
  - The Perfume Shop
  - Kruidvat
  - Trekpleister
  - Watsons
  - ICI Paris XL
  - Marionnaud
  - Drogas
  - Next Gen

Rules
─────
1. ALWAYS use the provided tools to get exact numbers. Never invent or estimate.
2. **Reply in the user's language.**  If they write in Italian, reply in Italian.
   If they write in English, reply in English.  Match their tone.
3. Be concise and visual.  Lead with the headline number in **bold**, then 1-2
   short bullets for context.  Skip preamble like "Here is the data:".
4. Use full BU names in replies (e.g. "Superdrug", not "SD").
5. Format numbers with thousands separators ("2,032", not "2032").
6. Always include context ("out of 7,234 total cases", "covers 28.3% of cases").
7. Call only the tools needed to answer the question.  Do NOT proactively call
   compare_bus() or query other BUs unless the user explicitly asks for a
   comparison or ranking — this is critical for staying within API rate limits.

CRITICAL — DO NOT ask for clarification when the user names one of the 10 BUs
above.  "How is Superdrug doing?" is UNAMBIGUOUS → call get_bu_coverage("Superdrug")
immediately.  Never present invented variants like "Superdrug / Savers" — there
is no such BU, those are two distinct BUs.

Only ask a clarifying question when:
  - The user's BU name does NOT match any of the 10 exactly or via known aliases
    (SD = Superdrug, KV = Kruidvat, WTR = Watsons, TPS = The Perfume Shop, etc.)
  - The question is genuinely vague (no BU mentioned at all, e.g. "How are we doing?")

Tool error handling: if a tool returns `{"error": "..."}`, share the actual error
message with the user.  Do NOT make up an explanation or hallucinate alternatives.

Answer format (for "how is X doing" questions)
──────────────────────────────────────────────
  **28.3%** automation coverage
  • 1,116 automated cases out of 3,949 total
  • Regression baseline: 92% covered (X/Y cases)
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
    config = types.GenerateContentConfig(
        tools=_TOOLS,
        system_instruction=_SYSTEM_INSTRUCTION.strip(),
        # Automatic function calling: the SDK executes our Python tools in-process
        # and feeds the results back to the model in a single round-trip.  Allow
        # enough hops for multi-step answers (e.g. list_bus → get_bu_coverage,
        # or a compare across BUs).
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=8,
        ),
        # Low temperature: this is a factual data assistant, not a creative one.
        temperature=0.2,
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
/* ── 1. The keyed container IS the FAB.  We size IT and let everything
       inside fill 100%.  This avoids Streamlit's inner layout quirks. ──── */
.st-key-ai_assistant_fab {
    position: fixed !important;
    bottom: 24px !important;
    left:   24px !important;
    z-index: 9999 !important;
    width: 48px !important;
    height: 48px !important;
    margin: 0 !important;
    padding: 0 !important;
    transition: width 0.30s cubic-bezier(0.4, 0, 0.2, 1);
}

/* Hover on the container → expand to a pill that snugly fits "💬 Ask Dexter" */
.st-key-ai_assistant_fab:hover {
    width: 158px !important;
}

/* ── 2. Every inner wrapper fills the container ─── */
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

/* Hide Streamlit's popover chevron/caret icon — the emoji is the only mark. */
.st-key-ai_assistant_fab button [data-testid="stIconMaterial"],
.st-key-ai_assistant_fab button svg {
    display: none !important;
}

/* ── 3. The button: fills 100%, round by default, morphs to pill on hover ── */
.st-key-ai_assistant_fab button {
    width: 100% !important;
    min-width: 100% !important;
    height: 48px !important;
    /* The content group is CENTRED.  Collapsed: only the emoji (label is
       zero-width) → emoji centred in the circle.  Expanded: the [emoji + label]
       group is centred in the pill. */
    padding: 0 !important;
    border-radius: 50% !important;
    overflow: hidden !important;
    white-space: nowrap !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    font-size: 18px !important;
    font-weight: 600 !important;
    line-height: 1 !important;
    background: #FF4B4B !important;
    color: #fff !important;
    border: none !important;
    box-shadow: 0 2px 10px rgba(255, 75, 75, 0.35) !important;
    transition:
        border-radius 0.30s cubic-bezier(0.4, 0, 0.2, 1),
        box-shadow    0.20s ease,
        background    0.20s ease;
}

/* Inner markdown wrapper: vertically centre the label, keep it on one line,
   and force WHITE text (the global markdown rules would otherwise colour it
   dark slate on the red pill). */
.st-key-ai_assistant_fab button > div[data-testid="stMarkdownContainer"] {
    display: flex !important;
    align-items: center !important;
    width: auto !important;
    flex: 0 0 auto !important;   /* emoji keeps its natural width (pinned left) */
}
.st-key-ai_assistant_fab button > div,
.st-key-ai_assistant_fab button p,
.st-key-ai_assistant_fab button span,
.st-key-ai_assistant_fab button * {
    color: #fff !important;
    line-height: 1 !important;
    padding: 0 !important;
    white-space: nowrap !important;
    text-overflow: clip !important;
    margin: 0 !important;
}

/* "Ask Dexter" label — a pseudo-element so the collapsed circle shows ONLY the
   emoji (no clipped letter peeking past the round edge).  It fades + slides in
   as the pill expands on hover. */
.st-key-ai_assistant_fab button::after {
    content: "Ask Dexter";
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap;
    /* A natural-width item placed AFTER the emoji.  Collapsed: zero-width &
       invisible (so the lone emoji centres in the circle).  Expanded: its real
       width + a gap appears, and the whole [emoji + label] group stays centred
       in the pill via the button's justify-content:center. */
    flex: 0 0 auto;
    max-width: 0;
    opacity: 0;
    overflow: hidden;
    transition: max-width 0.30s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.22s ease, margin-left 0.30s ease;
}
.st-key-ai_assistant_fab:hover button::after {
    max-width: 92px;
    opacity: 1;
    margin-left: 9px;
}

/* Hover on the keyed container — drives the pill morph + colour shift */
.st-key-ai_assistant_fab:hover button {
    border-radius: 26px !important;
    box-shadow: 0 4px 18px rgba(255, 75, 75, 0.45) !important;
    background: #E63E3E !important;
}

.st-key-ai_assistant_fab button:active {
    background: #D63030 !important;
    box-shadow: 0 2px 8px rgba(255, 75, 75, 0.35) !important;
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

    # Title row with a "new chat" icon button on the right.
    # The session_id is bumped on every new-chat click so widget keys change,
    # forcing Streamlit to treat every form/button as brand-new and avoiding
    # the popover quirk where stale widget state leaks across reruns.
    title_col, new_chat_col = st.columns([5, 1])
    title_col.markdown("### ✨ AI Assistant")
    if new_chat_col.button("📝", key="ai_new_chat",
                           help="Start a new chat",
                           use_container_width=True):
        st.session_state["ai_chat_messages"]   = []
        st.session_state["ai_chat_session_id"] = (
            st.session_state.get("ai_chat_session_id", 0) + 1
        )
        st.rerun()

    sid = st.session_state.get("ai_chat_session_id", 0)

    st.caption(
        "Ask anything about automation coverage, runs, bugs, or test stability. "
        "Numbers come live from TestRail — no made-up data."
    )

    msgs = st.session_state.get("ai_chat_messages", [])

    # ── empty-state suggestion chips ──────────────────────────────────────
    # Clicking a chip only QUEUES the message and reruns — the chips vanish
    # immediately (conversation no longer empty), so no sibling ever greys out.
    if not msgs:
        st.markdown("**Try asking:**")
        cols = st.columns(2)
        for i, (label, question) in enumerate(_SUGGESTIONS):
            if cols[i % 2].button(label, key=f"ai_sugg_{sid}_{i}",
                                  use_container_width=True):
                _queue_user_message(question)
                st.rerun()
        st.markdown("---")

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
        # The popover trigger button IS the FAB.  No `help=` so no tooltip
        # appears — the hover-expand morph speaks for itself.
        # Label is the emoji only; "Ask Dexter" is revealed via CSS ::after on
        # hover, so the collapsed circle never shows a clipped letter.
        with st.popover("💬", use_container_width=False):
            _render_chat_panel()
