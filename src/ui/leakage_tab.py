"""Leakage — defect leakage per release, and what production keeps losing.

Three tabs; app.py shows the last two only while the Leakage group is open:
  🐞 Leakage   one release: the ratios, the leaked incidents with the AI's
               analysis and an inspector, the UAT issues and the excluded;
  📈 Trend     the BU's last releases: the ratio, where incidents leak (Jira
               components), and how much of what leaks automation covers;
  🌍 All BUs   every BU side by side, with the leaks no automated test covers —
               the candidates for an extended, automated production suite.

The point (Matteo, 2026-09-28): see which parts of production keep leaking,
automate them, and watch the trend come down.  All history comes straight
from Jira, read-only.

The counting lives in `src/leakage.py`, the TestRail matching in
`src/leakage_match.py`, the AI in `src/leakage_ai.py`, its proposals and runs
in `src/leakage_store.py`.  Jira's data and the counts are shown plain; every
AI proposal is labelled as such.

Cost: Jira reads cached for the day; the AI runs once per incident, in the
background, and keeps its answer; the TestRail matching uses cases already
downloaded, so these tabs never call TestRail.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import altair as alt
import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from .. import freshness, gemini_client, jira_client
from .. import leakage as lk
from .. import leakage_ai as lai
from .. import leakage_export as lx
from .. import leakage_match as lm
from .. import leakage_store as ls
from ..freshness import DAY_TTL
from . import global_filter
from .styles import COLORS, section_title, stat_card

logger = logging.getLogger(__name__)

_KINDS = ("✅ Released", "🕓 Unreleased")
_TREND_RELEASES = 6
# A failed analysis is tried again on the next visit after this long; the
# button retries at once.
_RETRY_AFTER = 600
# How long an open page follows a running analysis live before it stops
# waiting (a batch took 20-40 s on the live app; three run in parallel).
_FOLLOW_SECONDS = 180

# What each leaked incident means for automation, in the order the charts
# stack it.  The first two are the extended production suite's candidates.
NO_TEST = "No test case"
MANUAL_ONLY = "Manual test only"
AUTOMATED_MISS = "Automated test missed it"
UNCLEAR = "Unclear"
NOT_UAT = "Not UAT-detectable"
NOT_ANALYSED = "Not analysed"
BUCKETS = (NO_TEST, MANUAL_ONLY, AUTOMATED_MISS, NOT_UAT, UNCLEAR, NOT_ANALYSED)
UNCOVERED = (NO_TEST, MANUAL_ONLY)
# The three gaps in colour, the rest in greys that lighten as the answer
# gets less certain.
_BUCKET_COLOURS = (COLORS["danger"], COLORS["warning"], COLORS["brand"],
                   "#94A3B8", "#CBD5E1", "#E9EDF3")


# ── small helpers ────────────────────────────────────────────────────────────
def _today():
    return freshness.business_day(time.time())


def _d(day) -> str:
    return f"{day.day} {day:%b %Y}" if day else "no date"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


def _ago(ts: float) -> str:
    secs = max(0, int(time.time() - ts))
    return f"{secs} s ago" if secs < 90 else f"{secs // 60} min ago"


def short_names(names: list[str]) -> dict[str, str]:
    """Release names without the part they all share: "EE_SAP_Release_2026Q2.Apr"
    and "…Q2.Jun" read as "2026Q2.Apr" and "2026Q2.Jun" on a narrow axis."""
    if len(names) < 2:
        return {n: n for n in names}
    prefix = os.path.commonprefix(names)
    suffix = os.path.commonprefix([n[::-1] for n in names])[::-1]
    # Cut at a separator only, never inside a token ("…Q2.1" / "…Q2.2").
    cut = max(prefix.rfind("_"), prefix.rfind(" ")) + 1
    tail = suffix if suffix.startswith((" ", "_")) else ""
    return {n: (n[cut:len(n) - len(tail)] or n) for n in names}


def _ratio_badge(value: float | None, limit: float) -> str:
    if value is None:
        return ""
    ok = value <= limit
    colour = COLORS["success"] if ok else COLORS["danger"]
    mark = "✓" if ok else "▲"
    return (f"<span style='font-size:12.5px;font-weight:650;color:{colour};"
            f"white-space:nowrap'>{mark} limit {limit:.0%}</span>")


def _group_and_releases():
    """(group, releases) for the BU in the top bar, or None after saying why."""
    if not jira_client.available():
        st.info("Leakage reads releases and incidents from Jira: add JIRA_URL, "
                "ATLASSIAN_USER and ATLASSIAN_API_KEY to the app secrets.")
        return None
    _scope, bu = global_filter.current()
    group = lk.group_for_bu(bu) if bu else None
    if group is None:
        st.info(f"No Jira project is mapped to **{bu}**: releases and incidents are "
                f"tracked per Jira project, and this Business Unit has none of its own. "
                f"Every BU is in 🌍 All BUs.")
        return None
    try:
        rels = lk.releases(group)
    except Exception as exc:
        logger.exception("Leakage: releases of %s unavailable", group.project)
        st.warning(f"The releases of {group.project} could not be read from Jira ({exc}).")
        return None
    if not rels:
        st.info(f"No fixVersion of {group.project} matches its release stream "
                f"(`{group.release_pattern}`).")
        return None
    return group, rels


# ── TestRail context, coverage and the AI ────────────────────────────────────
@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def _context(bus: tuple[str, ...], rows_json: str) -> dict[str, tuple[list, list]]:
    """{incident key: (linked [(CaseInfo, why)], candidates [CaseInfo])}."""
    index = lm.index_for(bus)
    out = {}
    for r in json.loads(rows_json):
        linked = lm.direct_links(r, index)
        out[r["key"]] = (linked, lm.candidates(r, index, {c.case_id for c, _w in linked}))
    return out


def _rows_json(rows: list[dict]) -> str:
    keep = ("key", "summary", "description", "steps", "expected", "components",
            "case_refs", "links")
    return json.dumps([{k: r.get(k) for k in keep} for r in rows], sort_keys=True)


def _cases_index(group: lk.Group) -> lm.Index | None:
    """The group's TestRail cases, or None while the dashboard data is not
    loaded.  Read once per render: every read of a cached value unpickles it."""
    try:
        return lm.index_for(group.bus)
    except Exception:
        logger.exception("Leakage: TestRail cases unavailable for %s", group.project)
        return None


def _prepare(data: lk.ReleaseLeakage, index: lm.Index | None) -> dict:
    """The TestRail context of each leaked incident (none without an index)."""
    ctx = {r["key"]: ([], []) for r in data.leaks}
    if not data.leaks or index is None:
        return ctx
    try:
        return _context(data.group.bus, _rows_json(data.leaks))
    except Exception:
        logger.exception("Leakage: TestRail matching unavailable for %s", data.group.project)
        return ctx


def _gap(final: dict, rec: ls.Record, linked: list, index: lm.Index | None) -> str:
    if final.get("uat_detectability") != lai.DETECTABLE:
        return "—"
    ids = [int(x) for x in re.findall(r"\d+", final.get("testrail_case") or "")]
    if not ids:
        ids = [c.case_id for c, _w in linked]
    if ids:
        statuses = [index.cases[i].status for i in ids if index and i in index.cases]
        return lm.gap(statuses) if statuses else "Case not in this BU's suites"
    return "Unclear" if (rec.ai or {}).get("testrail_matches") else NO_TEST


def bucket(final: dict, gap: str) -> str:
    """What one leaked incident means for automation (see BUCKETS).  Any
    automated case among those that should have caught it makes it a miss
    of automation ("Manual and automated" included), not a missing test."""
    verdict = final.get("uat_detectability")
    if not verdict:
        return NOT_ANALYSED
    if verdict == lai.NOT_DETECTABLE:
        return NOT_UAT
    if verdict != lai.DETECTABLE:
        return UNCLEAR
    if "automated" in gap.lower():
        return AUTOMATED_MISS
    if gap.startswith("Manual"):
        return MANUAL_ONLY
    return NO_TEST if gap == NO_TEST else UNCLEAR


def _assess(data: lk.ReleaseLeakage, index: lm.Index | None, ctx: dict):
    """(records, gaps, buckets) per leaked incident, from the store."""
    store = ls.store()
    records = {r["key"]: store.get(data.group.project, r["key"]) for r in data.leaks}
    gaps = {k: _gap(rec.final, rec, ctx[k][0], index) for k, rec in records.items()}
    buckets = {k: bucket(records[k].final, gaps[k]) for k in records}
    return records, gaps, buckets


def _pending(data: lk.ReleaseLeakage, ctx: dict) -> list[str]:
    """Incidents without a current proposal: never analysed, or edited in Jira
    (or offered other TestRail cases) since."""
    store = ls.store()
    return [r["key"] for r in data.leaks
            if (rec := store.get(data.group.project, r["key"])).ai is None
            or rec.ai_input != lai.input_hash(r, *ctx[r["key"]])]


def _job(data: lk.ReleaseLeakage, ctx: dict, keys: list[str]):
    """Claim the release's analysis and return the work, or None if it is
    already running (every visitor shares one run per release)."""
    store = ls.store()
    project, release = data.group.project, data.release.name
    rows = [r for r in data.leaks if r["key"] in keys]
    if not rows or not store.start_run(project, release, len(rows)):
        return None
    hashes = {r["key"]: lai.input_hash(r, *ctx[r["key"]]) for r in rows}

    def work(cooling: dict[str, float]) -> None:
        def on_batch(verdicts: dict[str, dict], model: str) -> None:
            for key, verdict in verdicts.items():
                store.put_ai(project, key, verdict, model, hashes[key])
            store.run_progress(project, release, len(verdicts), model)

        try:
            _results, _model, errors = lai.classify(rows, ctx, on_batch=on_batch,
                                                    cooling=cooling)
        except Exception as exc:
            logger.exception("Leakage: AI analysis of %s %s failed", project, release)
            errors = [f"The analysis stopped: {exc}"]
        store.finish_run(project, release, errors)

    return work


def _run_in_background(jobs: list) -> None:
    """Run the claimed analyses one release after another in one thread: the
    free tier's per-minute quota is shared, so several releases at once would
    only trade places in Google's queue."""
    jobs = [j for j in jobs if j is not None]
    if not jobs:
        return
    cooling = gemini_client.shared_cooldowns()
    try:
        # Warm the shared client here, in the script thread; the worker then
        # gets a cache hit.  A failure is the worker's to report, not the tab's.
        gemini_client.client(gemini_client.api_key())
    except Exception:
        logger.exception("Leakage: the Gemini client could not be created")

    def worker() -> None:
        for job in jobs:
            try:
                job(cooling)
            except Exception:       # one release's failure must not strand the next
                logger.exception("Leakage: a background analysis failed")

    threading.Thread(target=worker, name="leakage-ai", daemon=True).start()


def _due(data: lk.ReleaseLeakage, ctx: dict) -> list[str]:
    """What should be analysed now: pending incidents, unless the release is
    being analysed or its last run failed less than _RETRY_AFTER ago (a
    failing AI is not asked again on every click; the button retries)."""
    if not gemini_client.ready() or not data.leaks:
        return []
    run = ls.store().run(data.group.project, data.release.name)
    if run is not None and (run.state == ls.RUNNING or (
            run.state == ls.FAILED and time.time() - run.finished < _RETRY_AFTER)):
        return []
    return _pending(data, ctx)


def _running(targets: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The (project, release) pairs whose analysis is running now."""
    store = ls.store()
    return [(p, n) for p, n in targets
            if (run := store.run(p, n)) is not None and run.state == ls.RUNNING]


def _progress(slot, targets: list[tuple[str, str]]) -> None:
    """One bar for one or several running analyses (they run one after the
    other, so their totals add up)."""
    store = ls.store()
    runs = [r for p, n in targets if (r := store.run(p, n)) is not None]
    total = sum(r.total for r in runs)
    done = min(sum(r.done for r in runs), total)
    if len(runs) == 1:
        text = f"🤖 Analysing {total} incident{'s' if total != 1 else ''} with AI · {done} done"
    else:
        text = (f"🤖 Analysing {len(runs)} releases with AI · "
                f"{done} of {total} incidents done")
    slot.progress(done / total if total else 0.0, text=text)


def _rerun() -> None:
    """Redraw the tab: only its fragment when this run is a fragment rerun
    (a click in the tab), the page otherwise.  Streamlit refuses
    scope="fragment" during a full run — the first draw of the tab, which is
    when an analysis starts on its own."""
    ctx = get_script_run_ctx()
    st.rerun(scope="fragment" if ctx is not None and ctx.fragment_ids_this_run else "app")


def _follow(targets: list[tuple[str, str]], slot) -> None:
    """Keep the progress bar live while `targets` (project, release) are
    analysed, and redraw the tab when a batch lands or a run ends.  Every
    pass writes to the page, which is where Streamlit takes a click
    elsewhere: following never blocks the tab."""
    store = ls.store()

    def state():
        return tuple((r.state, r.done) if (r := store.run(p, n)) else None
                     for p, n in targets)

    seen = state()
    deadline = time.time() + _FOLLOW_SECONDS
    while time.time() < deadline:
        time.sleep(1.5)
        if state() != seen:
            _rerun()
        _progress(slot, targets)


def _models_panel() -> None:
    """Which model answered and why a stronger one did not."""
    status = gemini_client.model_status()
    rows = [{"Model": m, "Last outcome": status[m][0] if m in status else "not asked yet",
             "When": _ago(status[m][1]) if m in status else ""}
            for m in gemini_client.ANALYSIS_CHAIN]
    with st.expander("AI models, strongest first"):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("Each incident goes to the first model that answers; a model that "
                   "refuses (no quota on this plan, rate limit, not available) is skipped "
                   "for a while and the next one is asked.")


# ── 🐞 Leakage: one release ──────────────────────────────────────────────────
def _pick_release(group: lk.Group, rels: list[lk.Release]):
    """(release or None, the column where the export button goes)."""
    by_name = {r.name: r for r in rels}
    released = [r for r in reversed(rels) if r.released and r.date]   # newest first
    planned = [r for r in rels if not r.released]                     # soonest first
    c_kind, c_rel, c_export = st.columns([1.35, 3.2, 0.9], vertical_alignment="center")
    kind = c_kind.segmented_control(
        "Releases", _KINDS, default=_KINDS[0], required=True,
        key=f"lk_kind_{group.project}", label_visibility="collapsed") or _KINDS[0]
    pool = released if kind == _KINDS[0] else planned
    if not pool:
        c_rel.caption("No release of this kind in the project.")
        return None, c_export
    name = c_rel.selectbox(
        "Release", [r.name for r in pool],
        format_func=lambda n: (f"{n} · {'released' if by_name[n].released else 'planned'} "
                               f"{_d(by_name[n].date)}"),
        key=f"lk_rel_{group.project}_{kind}", label_visibility="collapsed")
    return by_name[name], c_export


def _cards(data: lk.ReleaseLeakage, records: dict[str, ls.Record],
           buckets: dict[str, str]) -> None:
    uat = len(data.uat)
    c1, c2, c3, c4 = st.columns(4)
    stat_card(c1, "Leakage ratio", _pct(data.ratio),
              badge_html=_ratio_badge(data.ratio, lk.LEAKAGE_THRESHOLD))
    c1.caption(f"{len(data.leaks)} leaked ÷ {uat} UAT issues")
    stat_card(c2, "Leakage · High & Highest", _pct(data.ratio_hh),
              badge_html=_ratio_badge(data.ratio_hh, lk.LEAKAGE_HH_THRESHOLD))
    c2.caption(f"{data.leaks_hh} leaked ÷ {uat} UAT issues")
    stat_card(c3, "UAT issues", uat)
    c3.caption(f"{data.uat_bugs} bugs · {data.uat_defects} defects")
    stat_card(c4, "Leaked incidents", len(data.leaks) if data.window else "—")
    c4.caption(f"{len(data.excluded)} more excluded by Delivery's rules"
               if data.window else "Not released yet")
    if not data.leaks:
        return
    counts = Counter(buckets.values())
    d1, d2 = st.columns(2)
    stat_card(d1, "Not covered by automation (AI)", sum(counts[b] for b in UNCOVERED))
    d1.caption(f"{counts[NO_TEST]} without a test case · {counts[MANUAL_ONLY]} manual only")
    stat_card(d2, "Automated test missed it (AI)", counts[AUTOMATED_MISS])
    d2.caption(f"{counts[NOT_UAT]} not UAT-detectable · "
               f"{counts[UNCLEAR] + counts[NOT_ANALYSED]} unclear or not analysed")


def _ai_status(data: lk.ReleaseLeakage, ctx: dict, records: dict[str, ls.Record]):
    """The analysis state and its one action; returns the progress slot while
    a run is going, for `_follow` to keep up to date."""
    if not gemini_client.ready():
        st.info("The AI analysis needs GEMINI_API_KEY in the secrets. "
                "The counts above do not depend on it.")
        return None
    run = ls.store().run(data.group.project, data.release.name)
    if run is not None and run.state == ls.RUNNING:
        slot = st.empty()
        _progress(slot, [(data.group.project, data.release.name)])
        return slot
    done = [r for r in records.values() if r.ai is not None]
    models = sorted({r.ai_model for r in done if r.ai_model})
    missing = _pending(data, ctx)
    left, right = st.columns([3.2, 1], vertical_alignment="center")
    left.markdown(
        f"<span style='font-size:13px;color:{COLORS['muted']}'>🤖 <b style='color:"
        f"{COLORS['ink']}'>{len(done)} of {len(records)}</b> incidents analysed by AI"
        + (f" · {html.escape(', '.join(models))}" if models else "")
        + "</span>", unsafe_allow_html=True)
    if run is not None and run.state == ls.FAILED:
        for e in run.errors:
            st.warning(e)
        st.caption(f"Last attempt {_ago(run.finished)}. Tried again automatically after "
                   f"{_RETRY_AFTER // 60} minutes, or now with the button.")
    # One call to action while something is left to analyse; once everything
    # has a proposal it steps back to a quiet button.
    key = f"lk_run_{data.group.project}_{data.release.name}"
    if missing:
        clicked = right.button(f"✨ Analyse {len(missing)} with AI", type="primary",
                               key=key, width="stretch")
    else:
        clicked = right.button("↻ Analyse again", type="tertiary", key=key, width="stretch")
    if clicked:
        _run_in_background([_job(data, ctx, missing or list(records))])
        _rerun()
    return None


def _incident_table(data: lk.ReleaseLeakage, records: dict[str, ls.Record],
                    gaps: dict[str, str]) -> None:
    rows = []
    for r in data.leaks:
        ai = records[r["key"]].ai or {}
        rows.append({
            "Incident": r["url"],
            "Summary": r["summary"],
            "Root cause (Jira)": r["root_cause"],
            "Verdict (AI)": ai.get("uat_detectability", "Not analysed"),
            "Category (AI)": ai.get("category", ""),
            "Confidence": ai.get("confidence"),
            "TestRail case": ai.get("testrail_case", ""),
            "Coverage gap": gaps[r["key"]],
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 height=min(38 + 35 * len(rows), 460), column_config={
                     "Incident": st.column_config.LinkColumn(
                         "Incident", display_text=r".*/browse/(.*)"),
                     "Summary": st.column_config.TextColumn("Summary", width="medium"),
                     "Confidence": st.column_config.ProgressColumn(
                         "Confidence", min_value=0.0, max_value=1.0, format="%.2f")})


def _case_rows(pairs: list[tuple[lm.CaseInfo, str, str]]) -> pd.DataFrame:
    return pd.DataFrame([{"Case": c.url, "Title": c.title, "Section": c.section,
                          "Status": c.status, "Match": how, "Why": why}
                         for c, how, why in pairs])


def _inspector(data: lk.ReleaseLeakage, records: dict[str, ls.Record], ctx: dict,
               index: lm.Index | None) -> None:
    section_title("Inspect an incident", top=14)
    by_key = {r["key"]: r for r in data.leaks}
    key = st.selectbox("Incident", list(by_key), key=f"lk_insp_{data.group.project}",
                       format_func=lambda k: f"{k} · {by_key[k]['summary'][:90]}",
                       label_visibility="collapsed")
    row, rec = by_key[key], records[key]
    ai = rec.ai or {}
    left, right = st.columns([1.05, 1], gap="large")
    with left:
        st.markdown(f"**Jira** · [{key}]({row['url']}) · {row['type']} · {row['priority']} · "
                    f"{row['status']}")
        st.caption(" · ".join(x for x in (
            f"Created {_d(row['created'].date())}" if row["created"] else "",
            f"Components: {', '.join(row['components'])}" if row["components"] else "",
            f"Labels: {', '.join(row['labels'])}" if row["labels"] else "",
            f"Environment: {row['environment']}" if row["environment"] else "",
            f"Root cause (EU): {row['root_cause']}" if row["root_cause"] else "",
        ) if x))
        st.markdown(f"<b>{html.escape(row['summary'].strip())}</b>", unsafe_allow_html=True)
        for field, label in (("description", "Description"), ("steps", "Steps to reproduce"),
                             ("actual", "Actual results"), ("expected", "Expected results")):
            with st.expander(label + ("" if row[field] else " (not filled)")):
                st.text(row[field] or "—")
        if row["links"]:
            st.caption("Linked: " + " · ".join(
                f"{lk_['key']} ({lk_['relation']})" for lk_ in row["links"]))
    with right:
        if not ai:
            st.info("No AI proposal yet for this incident.")
        else:
            st.markdown(f"**AI proposal** · {ai['uat_detectability']} · {ai['category']} · "
                        f"confidence {ai['confidence']:.2f}")
            if ai.get("functional_area"):
                st.caption(f"Functional area: {ai['functional_area']}")
            st.write(ai.get("rationale", ""))
            for flag in ai.get("flags", []):
                st.warning(flag)
            if ai.get("evidence"):
                st.markdown("**Evidence**\n" + "\n".join(
                    f"- *{e['field']}*: “{e['value']}” — {e['reason']}" for e in ai["evidence"]))
            if ai.get("missing_information"):
                st.caption("Would decide better with: " + ", ".join(ai["missing_information"]))
            if ai.get("corrective_actions"):
                st.markdown("**Suggested actions**\n" + "\n".join(
                    f"- {a}" for a in ai["corrective_actions"]))
            st.caption(f"{rec.ai_model} · {rec.ai_at}")
    linked, cands = ctx.get(key, ([], []))
    pairs = [(c, "Linked", why) for c, why in linked]
    known = {c.case_id for c, _h, _w in pairs}
    for m in ai.get("testrail_matches", []):
        if index and m["case_id"] in index.cases and m["case_id"] not in known:
            how = "AI, likely" if m["confidence"] >= lm.LIKELY else "AI, possible"
            pairs.append((index.cases[m["case_id"]], f"{how} ({m['confidence']:.2f})",
                          m["reason"]))
            known.add(m["case_id"])
    st.markdown("**TestRail cases**")
    if pairs:
        st.dataframe(_case_rows(pairs), hide_index=True, width="stretch", column_config={
            "Case": st.column_config.LinkColumn("Case", display_text=r".*/view/(\d+)")})
    else:
        st.caption("No linked case, and the AI chose none of the candidates.")
    rest = [c for c in cands if c.case_id not in known]
    if rest:
        with st.expander(f"The {len(rest)} other candidates the AI was shown"):
            st.dataframe(_case_rows([(c, "Candidate", "") for c in rest]).drop(columns="Why"),
                         hide_index=True, width="stretch", column_config={
                             "Case": st.column_config.LinkColumn("Case", display_text=r".*/view/(\d+)")})


def _issue_table(rows: list[dict], extra: dict[str, str] | None = None) -> None:
    if not rows:
        st.caption("None.")
        return
    extra = extra or {}
    df = pd.DataFrame([{
        "Issue": r["url"], **{label: r.get(k, "") for label, k in extra.items()},
        "Type": r["type"], "Created": r["created"].date() if r["created"] else None,
        "Priority": r["priority"], "Status": r["status"], "Summary": r["summary"],
    } for r in rows])
    st.dataframe(df, hide_index=True, width="stretch", height=min(38 + 35 * len(df), 380),
                 column_config={"Issue": st.column_config.LinkColumn(
                     "Issue", display_text=r".*/browse/(.*)")})


# Delivery's exclusion reasons, as the Excluded line sums them up.
_WHY_SHORT = {"Cancelled": "cancelled", "No root cause yet": "no root cause yet",
              "Root cause": "excluded root cause", "App component": "app",
              "App label": "app", "App team": "app"}


def _lists(data: lk.ReleaseLeakage) -> None:
    """The two lists behind the counts, one line each, the rows on demand."""
    c1, c2 = st.columns(2, gap="medium")
    with c1, st.expander(f"🐛 The {len(data.uat)} UAT issues counted"):
        _issue_table(data.uat)
        if data.uat_left_out:
            n = len(data.uat_left_out)
            st.caption(f"Not counted, as Delivery counts it: {n} defect{'s' if n != 1 else ''} "
                       f"of this fixVersion created before the previous release "
                       f"(listed in the Excel export).")
    if not data.window:
        return
    reasons = Counter(_WHY_SHORT.get(r["excluded_because"].split(":")[0], "other")
                      for r in data.excluded)
    why = " · ".join(f"{n} {reason}" for reason, n in reasons.most_common())
    with c2, st.expander(f"🚫 {len(data.excluded)} incidents excluded"
                         + (f" · {why}" if why else "")):
        _issue_table(data.excluded, {"Why": "excluded_because"})


@st.fragment
def render() -> None:
    picked = _group_and_releases()
    if picked is None:
        return
    group, rels = picked
    release, c_export = _pick_release(group, rels)
    if release is None:
        return
    try:
        with st.spinner("Reading the release…"):
            data = lk.analyse(group, rels, release.name, _today())
    except Exception as exc:
        logger.exception("Leakage: %s %s unavailable", group.project, release.name)
        st.warning(f"The release could not be read from Jira ({exc}). The counts are not "
                   f"shown rather than shown as zero; try again in a few minutes.")
        return

    index = _cases_index(group) if data.leaks else None
    ctx = _prepare(data, index)
    if due := _due(data, ctx):
        _run_in_background([_job(data, ctx, due)])
    records, gaps, buckets = _assess(data, index, ctx)

    c_export.download_button(
        "⬇ Excel", data=lambda: lx.workbook(
            data, records, gaps, {"Exported at": ls.now(),
                                  "AI models": ", ".join(sorted({r.ai_model for r in records.values()
                                                                 if r.ai_model})),
                                  "Categories": "proposed, to validate with the QA team",
                                  "Window": (f"{data.window.start} to "
                                             f"{'today' if data.window.open_ended else data.window.end}"
                                             if data.window else "not released")}),
        file_name=f"leakage_{group.project}_{release.name}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"lk_xlsx_{group.project}", width="stretch")

    _cards(data, records, buckets)
    _lists(data)                                  # the rows behind the cards
    slot = None
    if data.leaks:
        section_title("Leaked incidents", top=10)
        if index is None:
            st.caption("TestRail cases are not loaded yet: the matching appears once "
                       "the dashboard data has finished loading.")
        slot = _ai_status(data, ctx, records)
        _incident_table(data, records, gaps)
        _models_panel()
        _inspector(data, records, ctx, index)
    elif data.window:
        st.caption("No leaked incident in this window.")
    if slot is not None:                          # last: the whole tab is already drawn
        _follow([(group.project, release.name)], slot)


# ── 📈 Trend: the BU's last releases ─────────────────────────────────────────
def _ratio_chart(history: list[lk.ReleaseLeakage], short: dict[str, str]) -> None:
    st.markdown("**Leakage ratio by release**")
    rows = [{"release": short[d.release.name]
             + (" *" if d.window and d.window.open_ended else ""),
             "name": d.release.name,
             "ratio": d.ratio * 100 if d.ratio is not None else None,
             "leaked": len(d.leaks), "uat": len(d.uat), "hh": _pct(d.ratio_hh),
             "state": ("Above the limit" if d.ratio is not None
                       and d.ratio > lk.LEAKAGE_THRESHOLD else "Within the limit")}
            for d in history]
    df = pd.DataFrame(rows).dropna(subset=["ratio"])
    if df.empty:
        st.caption("No shipped release with UAT issues yet.")
        return
    bars = alt.Chart(df).mark_bar(size=34, cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=alt.X("release:N", sort=None, title=None,
                axis=alt.Axis(labelAngle=0, labelColor=COLORS["muted"], domain=False, ticks=False)),
        y=alt.Y("ratio:Q", title=None,
                axis=alt.Axis(format=".0f", labelExpr="datum.value + '%'", gridColor=COLORS["grid"],
                              labelColor=COLORS["muted"], domain=False, ticks=False)),
        color=alt.Color("state:N", title=None, legend=alt.Legend(orient="top"),
                        scale=alt.Scale(domain=["Within the limit", "Above the limit"],
                                        range=[COLORS["brand"], COLORS["danger"]])),
        tooltip=[alt.Tooltip("name:N", title="Release"),
                 alt.Tooltip("ratio:Q", title="Leakage ratio %", format=".1f"),
                 alt.Tooltip("leaked:Q", title="Leaked"), alt.Tooltip("uat:Q", title="UAT issues"),
                 alt.Tooltip("hh:N", title="High & Highest ratio")])
    limit = alt.Chart(pd.DataFrame({"y": [lk.LEAKAGE_THRESHOLD * 100]})).mark_rule(
        strokeDash=[5, 4], color=COLORS["muted"]).encode(y="y:Q")
    st.altair_chart((bars + limit).properties(height=220).configure_view(stroke=None),
                    width="stretch")


_OTHER_COMPONENTS = "Other components"


def _jql_url(keys: list[str]) -> str | None:
    """Jira's issue search on exactly these keys — the incidents a cell
    counts, Delivery's exclusions already applied — or None without Jira."""
    base = lk._base_url()
    if not base or not keys:
        return None
    return f"{base}/issues/?jql=" + quote(f"key in ({', '.join(keys)}) ORDER BY created DESC")


def heatmap_cells(history: list[lk.ReleaseLeakage],
                  short: dict[str, str]) -> tuple[pd.DataFrame, list[str]]:
    """(cells, component order) for the heatmap: one row per component and
    release with its leaked incidents' count and a Jira link to them.  The
    eight biggest components get a row each, the rest share one, where an
    incident with two of them is counted once (the link lists it once)."""
    keys: dict[tuple[str, str], dict[str, None]] = {}
    for d in history:
        for r in d.leaks:
            for c in (r["components"] or ["No component"]):
                keys.setdefault((c, d.release.name), {})[r["key"]] = None
    totals: Counter = Counter()
    for (c, _r), ks in keys.items():
        totals[c] += len(ks)
    top = [c for c, _n in totals.most_common(8)]
    cells: dict[tuple[str, str], dict[str, None]] = {}
    for (c, r), ks in keys.items():
        cells.setdefault((c if c in top else _OTHER_COMPONENTS, r), {}).update(ks)
    order = [*top, *([_OTHER_COMPONENTS] if len(totals) > len(top) else [])]
    df = pd.DataFrame([{"component": c, "release": short[r], "n": len(ks),
                        "url": _jql_url(list(ks))}
                       for (c, r), ks in cells.items()])
    return df, order


def _component_heatmap(history: list[lk.ReleaseLeakage], short: dict[str, str]) -> None:
    """Leaked incidents per Jira component and release: the areas that keep
    leaking are the ones an extended production suite should cover first.
    A cell opens its incidents in Jira."""
    st.markdown("**Where incidents leak** · leaked incidents per Jira component · "
                "click a cell for its incidents in Jira")
    df, order = heatmap_cells(history, short)
    if df.empty:
        st.caption("No leaked incident in these releases.")
        return
    releases = [short[d.release.name] for d in history]
    base = alt.Chart(df).encode(
        x=alt.X("release:N", sort=releases, title=None,
                axis=alt.Axis(labelAngle=0, labelColor=COLORS["muted"], domain=False, ticks=False,
                              orient="top")),
        y=alt.Y("component:N", sort=order, title=None,
                axis=alt.Axis(labelColor=COLORS["text"], labelLimit=240, labelOverlap=False,
                              domain=False, ticks=False)),
        href=alt.Href("url:N"),
        tooltip=[alt.Tooltip("component:N", title="Component"),
                 alt.Tooltip("release:N", title="Release"),
                 alt.Tooltip("n:Q", title="Leaked incidents")])
    # One hue, light to dark from zero; the count is written in every cell,
    # white on the darker ones.  Both layers carry the link, so a click on the
    # number opens it too.
    top_n = int(df["n"].max())
    cells = base.mark_rect(cornerRadius=3, stroke=COLORS["surface"], strokeWidth=2,
                           cursor="pointer").encode(
        color=alt.Color("n:Q", legend=None,
                        scale=alt.Scale(domain=[0, top_n], range=["#E0E7FF", COLORS["brand"]])))
    labels = base.mark_text(fontSize=12, fontWeight=600, cursor="pointer").encode(
        text="n:Q",
        color=alt.condition(alt.datum.n >= top_n * 0.6,
                            alt.value("#FFFFFF"), alt.value(COLORS["ink"])))
    # Links open in a new tab: by default Vega navigates the page itself,
    # which here is the app's own frame.
    st.altair_chart((cells + labels)
                    .properties(height=max(90, 30 * len(order)),
                                usermeta={"embedOptions": {"loader": {"target": "_blank"}}})
                    .configure_view(stroke=None), width="stretch")


def _bucket_scale() -> alt.Scale:
    return alt.Scale(domain=list(BUCKETS), range=list(_BUCKET_COLOURS))


def _coverage_chart(rows: list[dict], releases: list[str]) -> None:
    df = pd.DataFrame(rows)
    # A hairline of the surface between stacked segments keeps the light ones apart.
    chart = alt.Chart(df).mark_bar(size=34, stroke=COLORS["surface"], strokeWidth=1).encode(
        x=alt.X("release:N", sort=releases, title=None,
                axis=alt.Axis(labelAngle=0, labelColor=COLORS["muted"], domain=False, ticks=False)),
        y=alt.Y("n:Q", title=None, stack="zero",
                axis=alt.Axis(gridColor=COLORS["grid"], labelColor=COLORS["muted"], domain=False,
                              ticks=False, tickMinStep=1)),
        color=alt.Color("bucket:N", title=None, scale=_bucket_scale(),
                        legend=alt.Legend(orient="top", columns=3)),
        order=alt.Order("order:Q"),
        tooltip=[alt.Tooltip("release:N", title="Release"), alt.Tooltip("bucket:N", title=" "),
                 alt.Tooltip("n:Q", title="Leaked incidents")])
    st.altair_chart(chart.properties(height=240).configure_view(stroke=None), width="stretch")


@st.fragment
def render_trend() -> None:
    picked = _group_and_releases()
    if picked is None:
        return
    group, rels = picked
    try:
        with st.spinner("Reading the last releases…"):
            history = lk.trend(group, rels, _today(), n=_TREND_RELEASES)
    except Exception as exc:
        logger.exception("Leakage: release history of %s unavailable", group.project)
        st.warning(f"The release history could not be read from Jira ({exc}).")
        return
    if not history:
        st.info(f"No shipped release of {group.project} yet.")
        return
    short = short_names([d.release.name for d in history])
    releases = [short[d.release.name] for d in history]
    st.caption(f"{group.label} · last {len(history)} releases · * counted to date, the next "
               f"release has not shipped yet · dashed line: the {lk.LEAKAGE_THRESHOLD:.0%} limit")
    _ratio_chart(history, short)
    _component_heatmap(history, short)

    st.markdown("**What automation covers of what leaks** (AI)")
    index = _cases_index(group) if any(d.leaks for d in history) else None
    rows, due = [], []
    for d in history:
        ctx = _prepare(d, index)
        _records, _gaps, buckets = _assess(d, index, ctx)
        counts = Counter(buckets.values())
        rows += [{"release": short[d.release.name], "bucket": b, "n": counts[b], "order": i}
                 for i, b in enumerate(BUCKETS) if counts[b]]
        if keys := _due(d, ctx):
            due.append((d, ctx, keys))
    if rows:
        _coverage_chart(rows, releases)
    else:
        st.caption("No leaked incident in these releases.")
    running = _running([(group.project, d.release.name) for d in history])
    if running:
        slot = st.empty()
        _progress(slot, running)
        _follow(running, slot)
    elif due:
        # Not started on its own, unlike one release: six releases at once
        # are a real share of the free tier's daily quota.
        n = sum(len(k) for _d, _c, k in due)
        left, right = st.columns([3.2, 1], vertical_alignment="center")
        left.caption(f"{n} leaked incident{'s' if n != 1 else ''} of these releases "
                     f"{'are' if n != 1 else 'is'} not analysed yet: they count as "
                     f"“{NOT_ANALYSED}” above.")
        if right.button(f"✨ Analyse {n} with AI", type="primary", width="stretch",
                        key=f"lk_trend_run_{group.project}"):
            _run_in_background([_job(d, c, k) for d, c, k in due])
            _rerun()


# ── 🌍 All BUs ───────────────────────────────────────────────────────────────
def _history_of(group: lk.Group, today) -> dict:
    """One group's last releases, or the error that stopped the read."""
    try:
        rels = lk.releases(group)
        return {"group": group, "history": lk.trend(group, rels, today, n=_TREND_RELEASES)}
    except Exception as exc:
        logger.exception("Leakage: %s unavailable for All BUs", group.label)
        return {"group": group, "history": [], "error": str(exc)}


def _histories(today) -> list[dict]:
    """Every group's history, the groups read in parallel: one after another
    they wait on Jira six times over on a cold cache."""
    ctx = get_script_run_ctx()

    def read(group: lk.Group) -> dict:
        add_script_run_ctx(threading.current_thread(), ctx)   # for st.cache_data
        return _history_of(group, today)

    with ThreadPoolExecutor(max_workers=len(lk.GROUPS)) as pool:
        return list(pool.map(read, lk.GROUPS))


def _latest_row(g: lk.Group, hist: list[lk.ReleaseLeakage], counts: Counter) -> dict:
    d, prev = hist[-1], (hist[-2] if len(hist) > 1 else None)
    # The AI's figures only once every leak has its verdict: a partial count
    # would read as the answer (and Streamlit writes a missing number as
    # "None", which reads as zero), so "…" until then.
    complete = not counts[NOT_ANALYSED]
    return {
        "Business Unit": g.label,
        "Latest release": d.release.name + (" *" if d.window and d.window.open_ended else ""),
        "Leakage ratio": d.ratio * 100 if d.ratio is not None else None,
        "vs previous": ((d.ratio - prev.ratio) * 100
                        if prev and d.ratio is not None and prev.ratio is not None else None),
        "Trend": [x.ratio * 100 for x in hist if x.ratio is not None],
        "Leaked": len(d.leaks),
        "Not covered": str(sum(counts[b] for b in UNCOVERED)) if complete else "…",
        "Missed by automation": str(counts[AUTOMATED_MISS]) if complete else "…",
    }


def _missing_table(d: lk.ReleaseLeakage, records: dict[str, ls.Record],
                   buckets: dict[str, str]) -> None:
    """The leaks automation should have caught, worst gap first."""
    todo = sorted((r for r in d.leaks if buckets[r["key"]] in (*UNCOVERED, AUTOMATED_MISS)),
                  key=lambda r: BUCKETS.index(buckets[r["key"]]))
    rows = []
    for r in todo:
        ai = records[r["key"]].ai or {}
        rows.append({"Incident": r["url"], "Summary": r["summary"],
                     "What is missing": buckets[r["key"]],
                     "Area (AI)": ai.get("functional_area", ""),
                     "Suggested action (AI)": (ai.get("corrective_actions") or [""])[0],
                     "TestRail case": ai.get("testrail_case", "")})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 height=min(38 + 35 * len(rows), 380), column_config={
                     "Incident": st.column_config.LinkColumn(
                         "Incident", display_text=r".*/browse/(.*)"),
                     "Summary": st.column_config.TextColumn("Summary", width="medium"),
                     "Suggested action (AI)": st.column_config.TextColumn(
                         "Suggested action (AI)", width="medium")})


@st.fragment
def render_all_bus() -> None:
    if not jira_client.available():
        st.info("Leakage reads releases and incidents from Jira: add JIRA_URL, "
                "ATLASSIAN_USER and ATLASSIAN_API_KEY to the app secrets.")
        return
    with st.spinner("Reading every BU…"):
        histories = _histories(_today())

    # Each group's latest shipped release, analysed on its own like the one
    # a visitor opens in 🐞 Leakage: one release per BU, one after another.
    latest = {}
    for h in histories:
        if h["history"]:
            d = h["history"][-1]
            index = _cases_index(d.group) if d.leaks else None
            latest[d.group.project] = (d, index, _prepare(d, index))
    _run_in_background([_job(d, ctx, keys) for d, _i, ctx in latest.values()
                        if (keys := _due(d, ctx))])
    assessed = {p: _assess(d, index, ctx) for p, (d, index, ctx) in latest.items()}

    table = []
    for h in histories:
        g = h["group"]
        if not h["history"]:
            table.append({"Business Unit": g.label, "Trend": [], "Not covered": "",
                          "Missed by automation": "",
                          "Latest release": "Jira unavailable" if h.get("error") else "No release"})
            continue
        table.append(_latest_row(g, h["history"], Counter(assessed[g.project][2].values())))
    st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch", column_config={
        "Leakage ratio": st.column_config.NumberColumn(
            f"Leakage ratio (limit {lk.LEAKAGE_THRESHOLD:.0%})", format="%.1f%%"),
        "vs previous": st.column_config.NumberColumn("vs previous", format="%+.1f pts"),
        "Trend": st.column_config.LineChartColumn(f"Last {_TREND_RELEASES} releases", y_min=0),
        "Not covered": st.column_config.TextColumn("Not covered (AI)"),
        "Missed by automation": st.column_config.TextColumn("Missed by automation (AI)"),
    })
    st.caption("Each BU's latest shipped release (* counted to date: the next release has "
               "not shipped yet). Not covered: UAT-detectable leaks with no test case or a "
               "manual test only, the candidates for the extended production suite. The AI "
               "columns show … until every leak of the release is analysed.")

    running = _running([(p, d.release.name) for p, (d, _i, _c) in latest.items()])
    slot = st.empty() if running else None
    if slot is not None:
        _progress(slot, running)

    section_title("What automation is missing, BU by BU", top=12)
    for p, (d, _i, _c) in latest.items():
        records, _gaps, buckets = assessed[p]
        counts = Counter(buckets.values())
        todo = sum(counts[b] for b in (*UNCOVERED, AUTOMATED_MISS))
        label = (f"**{d.group.label}** · {todo} to automate or fix · {len(d.leaks)} leaked"
                 + (f" · {counts[NOT_ANALYSED]} not analysed yet" if counts[NOT_ANALYSED] else ""))
        with st.expander(label):
            if todo:
                _missing_table(d, records, buckets)
            elif not d.leaks:
                st.caption(f"No leaked incident since {d.release.name} shipped.")
            else:
                st.caption("None of the analysed leaks points at a missing or failing "
                           "automated test.")
    if slot is not None:
        _follow(running, slot)
