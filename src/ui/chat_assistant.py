"""Floating AI chat assistant — Gemini-powered Q&A over TestRail data.

A pill-shaped floating button at the bottom-left of every page opens a
popover-style chat panel (Rovo-like UX) where managers and QA leads can ask
natural-language questions such as "How is Superdrug doing?" or "What are the
top failing tests in Drogas?".

Architecture
────────────
Reliability-first, so we stay inside the free Gemini tier: a compact "LIVE
COVERAGE SNAPSHOT" of EVERY BU (coverage %, regression baseline, production
sanity, weakest areas, ranking) is pre-built from the same cached rule
evaluation the dashboard uses and injected into the system instruction.  Every
question is answered from that context in a SINGLE API call — no function
calling, so no question can fan out into TestRail or Jira requests.

The live-run tools (active runs, open bugs, test stability) were removed with
the Runs and Stability tabs: at the current TestRail rate limit one such
question could queue dozens of requests behind the dashboard's own download.

Privacy
───────
Only the user question and the coverage snapshot travel to the Gemini API.  Raw test-case
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

from .. import gemini_client
from ..bu_rules import ALL_RULES, BU_ALIASES
from ..freshness import DAY_TTL
from ..methodology import METHODOLOGY_FOR_LLM
from ..rules_engine import evaluate_rules
from . import coverage_tab
from .styles import COLORS

logger = logging.getLogger(__name__)


# ── Gemini — imported lazily in `gemini_client`, so the app boots without it ──
_GEMINI_AVAILABLE = gemini_client.AVAILABLE
types = gemini_client.types


# Only the most recent turns are sent to the model — the snapshot (rebuilt fresh
# on every request) is the source of truth, so old turns add noise, tokens and
# stale-number risk without improving answers.
_MAX_HISTORY_MSGS = 16

# Chat avatars — plain emoji glyphs (Streamlit's default colored circles clash
# with the styled message cards).
_AVATARS = {"user": "🧑", "assistant": "✨"}

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


_SYSTEM_INSTRUCTION_TEMPLATE = """
You are Dexter, the automation-coverage assistant for AS Watson's testing
platform.  You help managers and QA leads understand the state of test
automation across Business Units (BUs), and you can hold a real conversation
about it — follow-ups, comparisons, "why", "and the others?", etc.

The VALID Business Units are EXACTLY the ones listed in the "LIVE COVERAGE
SNAPSHOT" below — use those exact names and do NOT invent any others.  Note that
"Superdrug / Savers" is a real, separate entry (the suite of tests shared between
Superdrug and Savers); it is distinct from "Superdrug" and from "Savers".
Common aliases to map: {ALIASES}.

# WHERE YOUR DATA COMES FROM
Everything in the "LIVE COVERAGE SNAPSHOT" below is LIVE from TestRail — the exact
same pipeline the dashboard uses, so your numbers ALWAYS match the dashboard.
The numbers are exact: never round or estimate beyond what is given.  If a
specific number is NOT in the snapshot, say so plainly
("I don't have that exact number") instead of guessing.

# HOW TO ANSWER
  • "Coverage" on this dashboard means REGRESSION-BASELINE coverage (the KPI
    strip / Backlog convention) unless the user EXPLICITLY asks about the
    overall case universe.  For best/worst/ranking questions, quote the
    "PRIMARY RANKING" line from the snapshot VERBATIM — do not re-rank.
  • NEVER state a number that is not literally present in the snapshot.  If the exact figure is not there, say you don't have it —
    a made-up percentage is the worst possible answer.
  • Mobile-App-only entries (e.g. "Superdrug / Savers") have NO regression
    baseline: never name them best/worst for coverage; bring them up only for
    Mobile App questions.
  • Coverage, totals, automated counts, comparisons, rankings, gaps, the
    No-Regression baseline, the backlog breakdown (Backlog / To be Updated / N/A),
    frameworks (Java / Testim / Playwright) → answer DIRECTLY from the snapshot.
  • You have NO data on test runs, run pass rates, open bugs or test stability.
    If asked, say plainly that the dashboard does not currently show runs or
    stability — never estimate them from the coverage figures.

# HOW THE METRICS ARE CALCULATED  (use this to answer "how / why / what does X mean")
{METHODOLOGY}
Rules
─────
1. NEVER invent or estimate a number.  Use the snapshot — nothing else.
   If you genuinely don't have it, say so rather than guessing.
2. **Reply in the user's language** (Italian → Italian, English → English), match tone.
3. Be concise and conversational.  Lead with the headline number in **bold**, then
   1-3 short bullets.  No "Here is the data:" preamble.
4. Full BU names ("Superdrug", not "SD").  Thousands separators ("2,032").
5. Always give context ("1,116 of 3,949 cases", "28.3% covered").
6. Be proactive: add a one-line comparison or call out the weakest area when useful.
7. Don't ask to clarify when a BU is identifiable (name or alias) — just answer.
8. Cross-BU math (totals, averages, "overall"): use the precomputed GROUP TOTALS
   in the snapshot — do NOT re-add per-BU numbers yourself.  For any OTHER derived
   number (a difference, a ratio not provided), show the calculation inline
   ("1,886 − 1,342 = 544") so the user can verify it.

Answer shape (example for "how is X doing")
───────────────────────────────────────────
  **28.3%** automation coverage
  • 1,116 automated of 3,949 cases
  • No-Regression baseline: 92% (X/Y) · Production Sanity: 64% (X/Y)
  • Three RUNS, and they do not add up: Big No-Regression is the baseline;
    Small No-Regression is a SUBSET of it (already counted in it); Production
    Sanity is a SEPARATE baseline that may overlap it.  Never sum two runs.
  • Weakest area: <area> at 11%
"""

# Methodology comes from the shared module so the assistant explains the
# metrics exactly the way the in-app glossary does (one source of truth).
_SYSTEM_INSTRUCTION = (
    _SYSTEM_INSTRUCTION_TEMPLATE
    .replace("{METHODOLOGY}", METHODOLOGY_FOR_LLM)
    .replace("{ALIASES}", ", ".join(
        f"{alias}={bu}" for bu, aliases in BU_ALIASES.items() for alias in aliases))
)


# ── BU resolution ────────────────────────────────────────────────────────────
def _error_as_dict(fn):
    """Decorator: turn an exception in a snapshot helper into ``{"error": ...}``.

    The snapshot builder reads that key and skips the BU, so one BU whose data
    fails to load drops out of Dexter's context instead of blanking all of it.
    The failure is logged with its traceback — skipped, never silent.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                                        # noqa: BLE001
            logger.exception("%s failed", fn.__name__)
            return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    return wrapper


# ── Coverage helpers — the snapshot is built from these ─────────────────────
def _run_frames(run: str, scope: str, frames: dict | None) -> dict:
    """{(bu, scope): classified rows} for one Backlog run, once per snapshot.

    Every st.cache_data hit hands back a COPY of the whole payload, so reading
    it once per BU meant a dozen copies of the same frames per build.
    """
    key = ("run", run, scope)
    if frames is not None and key in frames:
        return frames[key]
    from . import backlog_tab as bl
    _summary, by_bu, _auto = bl._run_data(run, scope)
    if frames is not None:
        frames[key] = by_bu
    return by_bu


@_error_as_dict
def get_bu_coverage(bu: str, _frames: dict | None = None) -> dict:
    """Get automation coverage for a Business Unit.

    Returns total non-deprecated cases, automated count, coverage percentage,
    plus the top 15 functional areas (TestRail sections) and the regression
    baseline coverage (cases tagged with big_regr_desktop / big_regr_mobile).

    Args:
        bu: the canonical BU name, as the snapshot builder passes it.
    """
    if not any(r.bu == bu for r in ALL_RULES):
        return {"error": f"Unknown BU '{bu}'"}
    canonical = bu

    scope = next((r.scope for r in ALL_RULES if r.bu == canonical), "website")
    if scope == "mobile_app":
        # Mobile App is NOT pre-warmed (deferred scope): evaluate only THIS
        # BU's own suites — a tiny fetch — instead of pulling all 7 MAPP
        # suites into the warm-up via the coverage brief.
        rules = [r for r in ALL_RULES if r.bu == canonical and r.scope == scope]
    else:
        rules = [r for r in ALL_RULES if r.scope == scope]
    # _frames: per-call-site memo (the brief passes one dict for its whole
    # loop).  Every st.cache_data HIT deserializes a full COPY of the giant
    # ExpansionResult — 11 BUs used to mean 11 copies of the same object;
    # sharing the frames within one build makes it 1 copy per scope.
    key = tuple(r.name for r in rules)
    if _frames is not None and key in _frames:
        raw, auto = _frames[key]
    else:
        result = evaluate_rules(key)
        raw, auto = result.raw_cases, result.automated
        if _frames is not None:
            _frames[key] = (raw, auto)

    rules_bu  = [r for r in rules if r.bu == canonical]
    bu_suites = {r.suite_id for r in rules_bu}
    raw_bu  = raw[raw["suite_id"].isin(bu_suites)] if not raw.empty else raw
    auto_bu = auto[auto["bu"] == canonical] if not auto.empty else auto
    # Same convention as the Coverage tab: dedupe dual-framework
    # rows and drop other-BU cases on shared suites from the denominator.
    if not auto_bu.empty:
        auto_bu = auto_bu.drop_duplicates(subset=["case_id", "country_label", "device"])

    if raw_bu.empty:
        return {"error": f"No data loaded for {canonical}"}

    non_dep  = raw_bu[raw_bu["deprecated"] == False]  # noqa: E712
    non_dep, _n_other = coverage_tab._filter_to_bu_countries(non_dep, rules_bu)
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

    # The three runs, read from the Backlog tab's own cached frames — the rows
    # on screen, not a second expansion of them — so Dexter and the tab cannot
    # disagree.  This block used to take Small NR from `exp_base`, a variable
    # assigned only in the fallback below: in the normal path it raised
    # UnboundLocalError, the decorator turned that into {"error": …}, and the
    # snapshot silently dropped every BU the Backlog covers — from 2026-08-14
    # until 2026-09-26.  It also re-expanded Production Sanity per BU, which
    # is most of why building the snapshot took 164 s on Cloud.
    regression: dict[str, Any] = {}
    small_nr: dict[str, Any] = {}
    prod_sanity: dict[str, Any] = {}
    try:
        from . import backlog_tab as bl
        for scope_key in ("website", "next_gen"):
            exp = _run_frames(bl.RUN_BIG, scope_key, _frames).get((canonical, scope_key))
            if exp is None or exp.empty:
                continue
            regression = _regression_stats(exp)
            sub = _run_frames(bl.RUN_SMALL, scope_key, _frames).get((canonical, scope_key))
            if sub is not None and not sub.empty:
                small_nr = _regression_stats(sub)
            ps = _run_frames(bl.RUN_PS, scope_key, _frames).get((canonical, scope_key))
            if ps is not None and not ps.empty:
                prod_sanity = _regression_stats(ps)
            break
    except Exception:                                                   # noqa: BLE001
        logger.exception("get_bu_coverage: run lookup failed for %s", canonical)
    if not regression:
        # A BU the Backlog does not cover (Mobile-App-only ones): expand here.
        _nd, _ab, _ids, exp_base = coverage_tab._baseline_like_backlog(
            non_dep, auto_bu, rules_bu)
        if not exp_base.empty:
            regression = _regression_stats(exp_base)

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
        "small_no_regression":               small_nr,
    }


def _regression_stats(expanded) -> dict:
    """Regression figures from a classified baseline frame — ROW basis.

    The single place Dexter turns baseline rows into numbers, so its answers can
    never drift from the Backlog / Coverage tabs or the KPI strip.  Locked by
    tests/test_business_rules.py::TestDexterAgreesWithDashboard.
    """
    rows = int(len(expanded))
    auto = int((expanded["category"] == "automated").sum())
    return {
        "total_rows":       rows,
        "automated_rows":   auto,
        "total_cases":      int(expanded["case_id"].nunique()),
        "automated_unique": int(expanded.loc[expanded["category"] == "automated",
                                             "case_id"].nunique()),
        "coverage_pct":     round((auto / rows * 100) if rows else 0.0, 1),
    }


@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def _build_coverage_brief() -> str:
    """Build a compact markdown snapshot of CURRENT coverage for every BU.

    Injected into the system instruction so Gemini can answer coverage,
    comparison and gap questions from context in a SINGLE call — no function-calling
    round-trips.  Built from `get_bu_coverage`, which reads the three runs from
    the Backlog tab's own cached rows, so every figure here is one the screen
    shows.  Cached for the day, cleared with everything else by ↻ and by the
    daily reload; built in the background after a load (rules_engine).

    Streamlit keys this cache on THIS function's source, not on its callees':
    a fix inside `get_bu_coverage` alone left the broken snapshot of the day in
    place.  Changing this docstring is what retired it on 2026-09-26.
    """
    bus = sorted({r.bu for r in ALL_RULES})
    ranking: list[tuple[str, float]] = []
    blocks: list[str] = []

    # Regression-baseline / backlog breakdown — the SAME numbers as the Backlog
    # tab (Total rows, Automated, Backlog, To be Updated, N/A, and the framework
    # split Java / Testim / Playwright), so Dexter's regression answers line up
    # 1:1 with the dashboard.  Best-effort:
    # if it fails, the brief still carries the coverage numbers.
    backlog_by_bu: dict[str, dict] = {}
    try:
        from . import backlog_tab as bl
        summary, _, _ = bl._backlog_data()   # shared cache with the Backlog tab
        for _, row in summary.iterrows():
            backlog_by_bu[str(row["BU"])] = row.to_dict()
    except Exception:                                                   # noqa: BLE001
        logger.exception("Coverage brief: backlog summary failed")

    grand_total = grand_auto = 0   # accumulate for precomputed group totals

    _frames: dict = {}             # one giant-frame copy per SCOPE, not per BU
    for bu in bus:
        d = get_bu_coverage(bu, _frames=_frames)
        if not isinstance(d, dict) or "error" in d:
            blocks.append(f"## {bu}\n- (no data available right now)")
            continue
        ranking.append((d["business_unit"], d["coverage_pct"]))
        grand_total += int(d.get("total_cases") or 0)
        grand_auto  += int(d.get("automated_unique") or 0)
        lines = [f"## {d['business_unit']}"]
        bk = backlog_by_bu.get(d["business_unit"])
        if bk:
            lines.append(
                f"- No-Regression baseline (regression suite): "
                f"{int(bk['Automated']):,} automated of {int(bk['Total']):,} rows "
                f"({float(bk['Coverage %']):.1f}%) — Backlog {int(bk['Backlog']):,}, "
                f"To be Updated {int(bk['To be Updated']):,}, N/A {int(bk['Not Applicable']):,} "
                f"· Java {int(bk['Java']):,} / Testim {int(bk['TestIM']):,}"
                + (f" / Playwright {int(bk['Playwright']):,}"
                   if int(bk.get('Playwright') or 0) else "")
            )
        else:
            rb = d.get("regression_baseline") or {}
            if rb:
                lines.append(
                    f"- No-Regression baseline: {rb['coverage_pct']}% "
                    f"({rb['automated_rows']:,} automated of {rb['total_rows']:,} rows)"
                )
        lines.append(
            f"- Automated share of the WHOLE case universe (case basis — do NOT "
            f"call this 'coverage'): {d['coverage_pct']}% "
            f"({d['automated_unique']:,} automated of {d['total_cases']:,} cases)"
        )
        # Rows, not cases: the percentage beside them is row-based, and a
        # ratio in one unit next to a percentage in another is how a reader
        # ends up quoting a number that reconciles with nothing.
        ps = d.get("production_sanity") or {}
        if ps:
            lines.append(
                f"- Production Sanity run: {ps['coverage_pct']}% "
                f"({ps['automated_rows']:,} automated of {ps['total_rows']:,} "
                f"rows) — a SEPARATE baseline, never add it to the regression one"
            )
        sm = d.get("small_no_regression") or {}
        if sm:
            lines.append(
                f"- Small No-Regression run (the Release run): "
                f"{sm['coverage_pct']}% ({sm['automated_rows']:,} automated of "
                f"{sm['total_rows']:,} rows) — a SUBSET of the No-Regression "
                f"baseline above, already counted in it"
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
    # PRIMARY ranking = regression-baseline Cov.% — the dashboard's convention
    # (KPI strip + All-BU table).  The overall-universe ranking stays only as a
    # clearly-labeled secondary line: it was the source of "worst BU" answers
    # that contradicted the dashboard.
    regr_ranking = sorted(
        ((bu, float(v.get("Coverage %") or 0.0)) for bu, v in backlog_by_bu.items()),
        key=lambda x: -x[1],
    )
    mobile_only = [bu for bu, _ in ranking if bu not in backlog_by_bu]
    rank_lines = []
    if regr_ranking:
        rank_lines.append(
            "PRIMARY RANKING — regression-baseline coverage (the dashboard's "
            "convention; USE THIS for best/worst/ranking questions): "
            + " > ".join(f"{bu} {pct:.1f}%" for bu, pct in regr_ranking)
        )
    rank_lines.append(
        "Secondary — automated share of the whole case universe, CASE basis "
        "(this is not 'coverage'; use ONLY if the user explicitly asks about "
        "the whole universe rather than the baseline): "
        + " > ".join(f"{bu} {pct}%" for bu, pct in ranking)
    )
    if mobile_only:
        rank_lines.append(
            "Mobile-App-only entries (NO regression baseline — exclude from "
            "coverage rankings; relevant only to Mobile App questions): "
            + ", ".join(mobile_only)
        )
    rank_line = "\n".join(rank_lines)

    # Precomputed group totals — LLM arithmetic over many BUs is error-prone, so
    # cross-BU aggregates are computed here in Python and handed over verbatim.
    agg_lines = ["## GROUP TOTALS (precomputed — use these, do NOT re-add BU numbers yourself)"]
    if grand_total:
        # CASE basis, and summed across BUs — a case shared between two BUs is
        # counted twice.  It is therefore neither "coverage" (which is always
        # rows ÷ rows) nor a figure any screen shows: label it so Dexter cannot
        # quote it as either.  The row-basis group total is the line below.
        overall = grand_auto / grand_total * 100
        agg_lines.append(
            f"- Whole case universe, all BUs added up: {grand_auto:,} automated "
            f"of {grand_total:,} cases ({overall:.1f}%) — case basis, BU sums "
            f"overlap on shared suites. NOT coverage, do NOT quote as coverage."
        )
    if ranking:
        avg = sum(p for _, p in ranking) / len(ranking)
        agg_lines.append(
            f"- Average across the {len(ranking)} BUs of their case-basis "
            f"percentages: {avg:.1f}% (a simple mean, not a coverage figure)"
        )
    if regr_ranking:
        agg_lines.append(
            f"- Best (regression coverage): {regr_ranking[0][0]} "
            f"({regr_ranking[0][1]:.1f}%) · Worst: {regr_ranking[-1][0]} "
            f"({regr_ranking[-1][1]:.1f}%)")
    if backlog_by_bu:
        bk_tot  = sum(int(v.get("Total") or 0)     for v in backlog_by_bu.values())
        bk_auto = sum(int(v.get("Automated") or 0) for v in backlog_by_bu.values())
        bk_back = sum(int(v.get("Backlog") or 0)   for v in backlog_by_bu.values())
        bk_part = sum(int(v.get("Partially Automated") or 0)
                      for v in backlog_by_bu.values())
        bk_tbu  = sum(int(v.get("To be Updated") or 0) for v in backlog_by_bu.values())
        bk_na   = sum(int(v.get("Not Applicable") or 0)
                      for v in backlog_by_bu.values())
        agg_lines.append(
            f"- Regression baseline, all BUs combined: {bk_auto:,} automated of "
            f"{bk_tot:,} rows — Backlog {bk_back:,} (never automated anywhere), "
            f"Partially Automated {bk_part:,} (the case is automated in another "
            f"country/device), To be Updated {bk_tbu:,} (the test changed, so its "
            f"automation no longer matches it — these are NOT counted as "
            f"automated even where a script exists), N/A {bk_na:,}"
        )

    header = (
        "These are the CURRENT automation-coverage numbers, live from TestRail "
        "— the exact same data the dashboard shows. "
        "Use them directly to answer coverage / comparison / gap questions.\n"
    )
    return (f"{header}\n{rank_line}\n\n" + "\n".join(agg_lines) + "\n\n"
            + "\n\n".join(blocks))


# ── Gemini client / session ──────────────────────────────────────────────────
_get_api_key       = gemini_client.api_key


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
    """
    if not _GEMINI_AVAILABLE:
        return
    api_key = _get_api_key()
    if not api_key:
        return

    msgs = st.session_state.get("ai_chat_messages", [])
    if not msgs or msgs[-1]["role"] != "user":
        return  # nothing pending

    # Send only the most recent turns.  Long histories add noise (and tokens)
    # without helping factual answers — the snapshot, not the chat, is the source
    # of truth.  Trim to whole turns starting at a user message.
    window = msgs[-_MAX_HISTORY_MSGS:]
    while window and window[0]["role"] != "user":
        window = window[1:]
    contents = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part(text=m["content"])],
        )
        for m in window
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
    else:
        # NEVER let the model improvise when the snapshot failed to build — the
        # instruction references a snapshot, so make its absence explicit.
        system_instruction += (
            "\n\n# LIVE COVERAGE SNAPSHOT\n"
            "UNAVAILABLE — the coverage data could not be loaded right now. "
            "Tell the user plainly that the live numbers are temporarily "
            "unavailable and to retry in a minute (or refresh the dashboard). "
            "Do NOT state any coverage number from memory."
        )

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        # Near-deterministic decoding: this is a factual data assistant reading
        # numbers out of its context — sampling variety only hurts here.
        temperature=0.1,
        top_p=0.9,
    )

    # The fallback walk and its cooldowns live in `gemini_client` — shared with
    # AI Test Design, so a model one feature exhausted is skipped by the other.
    cooling: dict[str, float] = st.session_state.setdefault(
        gemini_client.COOLDOWN_KEY, {})
    result = gemini_client.generate(contents, config, _models_to_try(), cooling)
    used_model = result.model
    if used_model is not None:
        reply = result.text or "(empty response)"
    else:
        reply = gemini_client.failure_message(result.error)

    st.session_state["ai_last_used_model"] = used_model
    msgs.append({"role": "assistant", "content": reply})


# ── UI ───────────────────────────────────────────────────────────────────────
# Streamlit ≥1.39 adds the class `st-key-{key}` on any element with a custom key.
# We anchor our CSS on that — far more robust than `:has()` tricks.
_FAB_CSS = """
<style>
/* ── 1. The keyed container IS the FAB.  Idle, it is a small 48px circle showing
       only the sparkle icon — it just peeks at the bottom-left so it never
       overlaps the page.  On hover (or while the chat is open) it WIDENS to the
       full "✨ Ask Dexter" pill and is clickable.  The left edge is fixed and it
       grows rightward, so the cursor never slips off mid-grow (no hover flicker)
       and the icon never moves. */
.st-key-ai_assistant_fab {
    position: fixed !important;
    bottom: 24px !important;
    left:   24px !important;
    z-index: 9999 !important;
    width: 48px !important;            /* collapsed: a small icon circle */
    height: 48px !important;
    margin: 0 !important;
    padding: 0 !important;
    transition: width 0.30s cubic-bezier(0.4, 0, 0.2, 1) !important;
}
.st-key-ai_assistant_fab:hover,
.st-key-ai_assistant_fab:has(button[aria-expanded="true"]) {
    width: 158px !important;           /* expanded: the full pill */
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
    padding: 0 15px !important;
    border-radius: 24px !important;
    overflow: hidden !important;
    white-space: nowrap !important;
    display: flex !important;
    align-items: center !important;
    /* flex-start pins the icon at the left (15px ≈ centred in the 48px circle).
       As the container widens, the label simply reveals to the icon's right and
       the icon stays put. */
    justify-content: flex-start !important;
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
/* Collapsed: the label is fully HIDDEN (opacity 0) so not even its first letter
   peeks past the icon circle.  It fades in only once expanded (hover / chat
   open), independent of the width clip. */
.st-key-ai_assistant_fab button [data-testid="stMarkdownContainer"] {
    opacity: 0 !important;
    transition: opacity 0.18s ease 0.04s !important;
}
.st-key-ai_assistant_fab:hover button [data-testid="stMarkdownContainer"],
.st-key-ai_assistant_fab:has(button[aria-expanded="true"]) button [data-testid="stMarkdownContainer"] {
    opacity: 1 !important;
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

/* ── 3. The chat panel that opens above the FAB ─────────────────────────── */
/* Scoped with :has() to the popover that CONTAINS the chat form — the app now
   has other popovers (methodology, data quality) in the utility bar, and an
   unscoped rule would dress them as chat panels.  Selectbox/multiselect
   dropdowns use baseweb menu/listbox, not stPopoverBody, so they stay untouched. */
[data-testid="stPopoverBody"]:has([class*="st-key-ai_chat_form_"]) {
    min-width: 440px;
    max-width: min(500px, 92vw);
    max-height: min(620px, 76vh);
    overflow-y: auto;
    padding: 16px 18px 18px !important;   /* bottom ≥ footer height, or it clips */
}

/* Hide the "Press Enter to submit form" helper — visual noise in a chat box. */
[data-testid="stPopoverBody"]:has([class*="st-key-ai_chat_form_"]) [data-testid="InputInstructions"] {
    display: none !important;
}

/* Chat cards — clean full-width message cards: assistant = white, user = a
   soft warm tint.  Avatars are the tidy emoji set via st.chat_message(avatar=)
   (no colored default circles).  Hex values mirror styles.py tokens. */
[data-testid="stPopoverBody"]:has([class*="st-key-ai_chat_form_"]) [data-testid="stChatMessage"] {
    background: #FFFFFF;
    border: 1px solid #E6EAF1;
    border-radius: 14px;
    padding: 12px 14px !important;
    margin: 4px 0 !important;
    box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
/* User messages — both testid generations covered (old chatAvatarIcon-*,
   new stChatMessageAvatar*). */
[data-testid="stPopoverBody"]:has([class*="st-key-ai_chat_form_"]) [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]),
[data-testid="stPopoverBody"]:has([class*="st-key-ai_chat_form_"]) [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
    background: #FFF6F6;
    border-color: #FFDCDC;
}
[data-testid="stPopoverBody"]:has([class*="st-key-ai_chat_form_"]) [data-testid="stChatMessage"] p {
    font-size: 13.5px;
    line-height: 1.55;
}

/* Chat input — rounded field so it matches the bubbles. */
[data-testid="stPopoverBody"]:has([class*="st-key-ai_chat_form_"]) [data-baseweb="input"],
[data-testid="stPopoverBody"]:has([class*="st-key-ai_chat_form_"]) [data-baseweb="base-input"] {
    border-radius: 12px !important;
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

    if not msgs:
        # ── welcome hero (empty chat) — centred, airy, no grey box ────────────
        st.markdown(
            f"<div style='text-align:center;padding:14px 6px 8px'>"
            f"<div style='width:46px;height:46px;margin:0 auto 10px;border-radius:14px;"
            f"display:inline-flex;align-items:center;justify-content:center;font-size:22px;"
            f"background:linear-gradient(135deg,#FF6B6B 0%,#E63E3E 100%);"
            f"box-shadow:0 6px 16px rgba(255,75,75,0.32)'>✨</div>"
            f"<div style='font-size:16.5px;font-weight:800;color:{COLORS['ink']};"
            f"letter-spacing:-0.01em'>Hi, I'm Dexter</div>"
            f"<div style='font-size:12px;color:{COLORS['muted']};margin-top:5px;"
            f"line-height:1.55'>Ask me anything about coverage, runs, bugs or flaky "
            f"tests, <br>numbers come live from TestRail and match the dashboard.</div>"
            f"<div style='font-size:11px;color:{COLORS['faint']};margin-top:12px;"
            f"font-style:italic;white-space:nowrap'>“How is Superdrug doing?”"
            f"&nbsp;·&nbsp;“Compare all BUs”&nbsp;·&nbsp;“Open bugs in Watsons Turkey”</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        # ── compact header once a conversation exists ─────────────────────────
        # Deleting bumps the session_id so widget keys change, forcing Streamlit
        # to treat every form/button as brand-new (avoids stale widget state
        # leaking across reruns inside the popover).
        head_l, head_r = st.columns([7, 3], vertical_alignment="center")
        # Inline <span>s, not block <div>s: Streamlit gives the markdown
        # container a -1rem bottom margin on the assumption that a <p> inside
        # supplies +1rem.  Block HTML gets no such margin, so the block reports
        # 22px instead of 38px and the header sat 8px below the "Delete chat"
        # button it shares a centred row with.  (Measured; identical rendering.)
        head_l.markdown(
            f"<span style='display:inline-flex;align-items:center;gap:10px'>"
            f"<span style='width:38px;height:38px;border-radius:12px;flex:0 0 auto;"
            f"display:inline-flex;align-items:center;justify-content:center;font-size:18px;"
            f"background:linear-gradient(135deg,#FF6B6B 0%,#E63E3E 100%);"
            f"box-shadow:0 3px 10px rgba(255,75,75,0.35)'>✨</span>"
            f"<span style='display:inline-block'>"
            f"<span style='display:block;font-size:17px;font-weight:800;color:{COLORS['ink']};"
            f"letter-spacing:-0.01em;line-height:1.1;white-space:nowrap'>Dexter</span>"
            f"<span style='display:block;font-size:11px;color:{COLORS['muted']};margin-top:2px;"
            f"white-space:nowrap'>AI coverage assistant</span>"
            f"</span></span>",
            unsafe_allow_html=True,
        )
        if head_r.button("Delete chat", key="ai_delete_chat",
                         width="stretch"):
            st.session_state["ai_chat_messages"]   = []
            st.session_state["ai_chat_session_id"] = sid + 1
            st.rerun()
        st.markdown(
            f"<div style='height:1px;background:{COLORS['border']};margin:10px 0 6px'></div>",
            unsafe_allow_html=True,
        )

    # ── conversation history ──────────────────────────────────────────────
    # Explicit emoji avatars — clean glyphs instead of Streamlit's colored
    # default circles (which clash with the bubble styling).
    for msg in msgs:
        with st.chat_message(msg["role"], avatar=_AVATARS.get(msg["role"])):
            st.markdown(msg["content"])

    # ── generate the reply for a freshly-queued user message ──────────────
    if msgs and msgs[-1]["role"] == "user":
        n_before = len(msgs)
        with st.chat_message("assistant", avatar=_AVATARS["assistant"]), \
                st.spinner("Thinking…"):
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
        submitted = cols[1].form_submit_button("→", width="stretch")
    if submitted and user_input.strip():
        _queue_user_message(user_input.strip())
        st.rerun()

    # ── footer ────────────────────────────────────────────────────────────
    # padding-bottom (not margin) so the last line never sits flush against —
    # or clipped by — the popover's bottom edge.
    st.markdown(
        f"<div style='text-align:right;font-size:10.5px;color:{COLORS['muted']};"
        f"margin-top:4px;padding-bottom:4px;line-height:1'>"
        f"{_display_model()} · Uses AI</div>",
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
        with st.popover("Ask Dexter", width="content"):
            _render_chat_panel()
