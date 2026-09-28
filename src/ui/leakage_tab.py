"""Leakage tab — defect leakage per release, and why each incident escaped.

Follows the global BU selector.  The release is picked here, from the BU's
Jira project (released or unreleased; the latest released by default).  All
the counting lives in `src/leakage.py`, the TestRail matching in
`src/leakage_match.py`, the AI in `src/leakage_ai.py`, the Key QA's decisions
in `src/leakage_store.py`.

What is what, on screen:
  * Jira source data and counts — plain;
  * the AI's proposals — in columns and panels labelled "AI";
  * the Key QA's decisions — the editable columns (✎), with who and when.

Cost: Jira reads cached for the day; the AI runs once per incident and keeps
its answer; the TestRail matching uses cases already downloaded — this tab
never calls TestRail.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from collections import Counter

import altair as alt
import pandas as pd
import streamlit as st

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
_VIEWS = ("🧾 Incidents", "📊 Insights", "🐛 UAT issues", "🚫 Excluded", "🌍 All BUs")


def _today():
    return freshness.business_day(time.time())


def _d(day) -> str:
    return f"{day.day} {day:%b %Y}" if day else "no date"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


# ── release picker ───────────────────────────────────────────────────────────
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


def _uat_rule(data: lk.ReleaseLeakage) -> str:
    since = (f"the Defects created since {data.previous.name} shipped "
             f"({_d(data.previous.date)})" if data.previous else "its Defects")
    return f"**UAT issues:** every Bug with fixVersion {data.release.name}, and {since}."


def _window_caption(data: lk.ReleaseLeakage) -> None:
    w, rel = data.window, data.release
    if w is None:
        st.caption(f"**{rel.name}** has not been released yet (planned {_d(rel.date)}): "
                   f"UAT issues so far, no post-release window yet. {_uat_rule(data)}")
        return
    if w.open_ended:
        end = "today"
        tail = (f", until {w.following.name} ships (planned {_d(w.following.date)})"
                if w.following else ", no later release planned yet")
    else:
        end, tail = _d(w.end), f", when {w.following.name} shipped"
    st.caption(f"{_uat_rule(data)} **Leaked:** Production Incidents from "
               f"{_d(w.start)} to {end}{tail}, counted with Delivery's rules.")


# ── TestRail context and AI ──────────────────────────────────────────────────
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


def _ai_key(data: lk.ReleaseLeakage) -> str:
    return f"lk_ai_{data.group.project}_{data.release.name}"


def _run_ai(data: lk.ReleaseLeakage, ctx: dict, keys: list[str]) -> None:
    store = ls.store()
    rows = [r for r in data.leaks if r["key"] in keys]
    with st.spinner(f"Analysing {len(rows)} incident{'s' if len(rows) != 1 else ''} "
                    f"with AI — about ten seconds per six…"):
        results, model, errors = lai.classify(rows, ctx)
    for r in rows:
        if r["key"] in results:
            store.put_ai(data.group.project, r["key"], results[r["key"]], model or "",
                         lai.input_hash(r, *ctx[r["key"]]))
    st.session_state[_ai_key(data)] = errors


def _auto_ai(data: lk.ReleaseLeakage, ctx: dict) -> None:
    """Analyse what has no current proposal — once per release per process,
    so a failing AI is not asked again on every click (the button retries)."""
    store = ls.store()
    project, release = data.group.project, data.release.name
    pending = [r["key"] for r in data.leaks
               if (rec := store.get(project, r["key"])).ai is None
               or rec.ai_input != lai.input_hash(r, *ctx[r["key"]])]
    if pending and gemini_client.ready() and not store.attempted(project, release):
        store.mark_attempted(project, release)
        _run_ai(data, ctx, pending)


def _gap(final: dict, rec: ls.Record, linked: list, index: lm.Index | None) -> str:
    if final.get("uat_detectability") != lai.DETECTABLE:
        return "—"
    ids = [int(x) for x in re.findall(r"\d+", final.get("testrail_case") or "")]
    if not ids:
        ids = [c.case_id for c, _w in linked]
    if ids:
        statuses = [index.cases[i].status for i in ids if index and i in index.cases]
        return lm.gap(statuses) if statuses else "Case not in this BU's suites"
    return "Unclear" if (rec.ai or {}).get("testrail_matches") else "No test case"


# ── summary cards ────────────────────────────────────────────────────────────
def _ratio_badge(value: float | None, limit: float) -> str:
    if value is None:
        return ""
    ok = value <= limit
    colour = COLORS["success"] if ok else COLORS["danger"]
    mark = "✓" if ok else "▲"
    return (f"<span style='font-size:12.5px;font-weight:650;color:{colour};"
            f"white-space:nowrap'>{mark} limit {limit:.0%}</span>")


def _cards(data: lk.ReleaseLeakage, finals: dict[str, dict], gaps: dict[str, str],
           records: dict[str, ls.Record]) -> None:
    uat = len(data.uat)
    c1, c2, c3, c4 = st.columns(4)
    stat_card(c1, "Leakage ratio", _pct(data.ratio),
              badge_html=_ratio_badge(data.ratio, lk.LEAKAGE_THRESHOLD))
    c1.caption(f"{len(data.leaks)} leaked ÷ {uat} UAT issues")
    stat_card(c2, "Leakage · High & Highest", _pct(data.ratio_hh),
              badge_html=_ratio_badge(data.ratio_hh, lk.LEAKAGE_HH_THRESHOLD))
    c2.caption(f"{data.leaks_hh} leaked ÷ {uat} UAT issues")
    stat_card(c3, "UAT issues", uat)
    older = len(data.uat_left_out)
    c3.caption(f"{data.uat_bugs} bugs · {data.uat_defects} defects"
               + (f" · {older} older defect{'s' if older != 1 else ''} not counted"
                  if older else ""))
    stat_card(c4, "Leaked incidents", len(data.leaks) if data.window else "—")
    c4.caption(f"{len(data.excluded)} more excluded by Delivery's rules"
               if data.window else "Not released yet")
    if not data.leaks:
        return
    verdicts = Counter(f.get("uat_detectability") or "Not analysed" for f in finals.values())
    reviewed = sum(1 for r in records.values() if r.status != ls.PENDING)
    gap_counts = Counter(g for g in gaps.values() if g != "—")
    manual = sum(v for k, v in gap_counts.items() if k.startswith("Manual"))
    automated = sum(v for k, v in gap_counts.items() if k.startswith("Automated"))
    d1, d2, d3 = st.columns(3)
    stat_card(d1, "UAT-detectable", verdicts[lai.DETECTABLE])
    d1.caption(f"{verdicts[lai.NOT_DETECTABLE]} not UAT-detectable · "
               f"{verdicts[lai.NEEDS_REVIEW] + verdicts['Not analysed']} to decide")
    stat_card(d2, "Reviewed by the Key QA", f"{reviewed} of {len(data.leaks)}")
    d2.caption("The rest are the AI's proposals")
    stat_card(d3, "Coverage gaps", sum(gap_counts.values()))
    d3.caption(f"{manual} manual · {automated} automated · "
               f"{gap_counts['No test case']} without a test case")


# ── incidents: review table and inspector ────────────────────────────────────
def _editor_key(data: lk.ReleaseLeakage) -> str:
    ver = st.session_state.get(f"lk_ver_{data.group.project}", 0)
    return f"lk_ed_{data.group.project}_{data.release.name}_{ver}"


def _ai_status(data: lk.ReleaseLeakage, ctx: dict, records: dict[str, ls.Record]) -> None:
    done = [r for r in records.values() if r.ai is not None]
    models = sorted({r.ai_model for r in done if r.ai_model})
    left, right = st.columns([4, 1.3], vertical_alignment="center")
    if not gemini_client.ready():
        left.info("The AI analysis needs GEMINI_API_KEY in the secrets — the counts "
                  "above do not depend on it.")
        return
    errors = st.session_state.get(_ai_key(data)) or []
    text = (f"🤖 AI proposals for {len(done)} of {len(records)} incidents"
            + (f" · {', '.join(models)}" if models else ""))
    left.caption(text + ".  Proposals, not decisions: confirm or change them below.")
    for e in errors:
        left.warning(e)
    missing = [k for k, r in records.items() if r.ai is None]
    label = f"🤖 Analyse {len(missing)}" if missing else "↻ Re-analyse all"
    if right.button(label, key=f"lk_run_{data.group.project}_{data.release.name}",
                    width="stretch"):
        _run_ai(data, ctx, missing or list(records))
        st.rerun(scope="fragment")


def _review_table(data: lk.ReleaseLeakage, records: dict[str, ls.Record],
                  gaps: dict[str, str]) -> None:
    rows = []
    for r in data.leaks:
        rec = records[r["key"]]
        ai, final = rec.ai or {}, rec.final
        # Only what a review decision needs, so the ✎ columns fit on screen.
        # They start as the AI's proposal, so separate "AI verdict / category"
        # columns would repeat them; the AI's original stays in the inspector
        # below, in the history and in the Excel.  The confidence says where
        # to look first.
        rows.append({
            "Incident": r["url"],
            "Summary": r["summary"],
            "Root cause (Jira)": r["root_cause"],
            "AI confidence": ai.get("confidence"),
            "Verdict": final.get("uat_detectability") or None,
            "Category": final.get("category") or None,
            "TestRail case": final.get("testrail_case", ""),
            "Coverage gap": gaps[r["key"]],
            "Review": rec.status,
            "Comment": "",
        })
    base = pd.DataFrame(rows, index=[r["key"] for r in data.leaks])
    clash = [k for k, rec in records.items()
             if (f := rec.final).get("category") in lai.VERDICT_OF
             and f.get("uat_detectability") in (lai.DETECTABLE, lai.NOT_DETECTABLE)
             and lai.VERDICT_OF[f["category"]] not in (f["uat_detectability"], lai.NEEDS_REVIEW)]
    if clash:
        st.warning(f"Verdict and category disagree on {', '.join(clash)}: the category "
                   f"belongs to the other verdict — change one of the two.")
    # A form: edits and the Save travel together.  Outside one, the first
    # click on Save was spent committing the cell being edited (a rerun of its
    # own) and did nothing visible — found in the preview on 2026-09-28.
    with st.form(key=f"lk_form_{_editor_key(data)}", border=False):
        c_who, c_save, c_note = st.columns([1.6, 1, 3], vertical_alignment="bottom")
        reviewer = c_who.text_input("Reviewer", key="lk_reviewer",
                                    placeholder="Your name — kept with each change")
        save = c_save.form_submit_button("💾 Save review", type="primary", width="stretch")
        c_note.caption("✎ columns are the Key QA's.  Saved reviews are kept until the "
                       "app restarts — there is no permanent archive yet; export to "
                       "Excel to keep them.")
        edited = st.data_editor(
            base, key=_editor_key(data), hide_index=True, width="stretch",
            height=min(38 + 35 * len(base), 460),
            disabled=[c for c in base.columns
                      if c not in ("Verdict", "Category", "TestRail case", "Review", "Comment")],
            column_config={
                "Incident": st.column_config.LinkColumn("Incident", display_text=r".*/browse/(.*)"),
                "Summary": st.column_config.TextColumn("Summary", width="medium"),
                "AI confidence": st.column_config.ProgressColumn(
                    "AI conf.", min_value=0.0, max_value=1.0, format="%.2f"),
                "Verdict": st.column_config.SelectboxColumn("Verdict ✎", options=list(lai.VERDICTS)),
                "Category": st.column_config.SelectboxColumn(
                    "Category ✎", options=list(lai.CATEGORIES)),
                "TestRail case": st.column_config.TextColumn(
                    "TestRail case ✎", help="C-numbers, comma separated",
                    validate=r"^\s*(C?\d+(\s*,\s*C?\d+)*)?\s*$"),
                "Review": st.column_config.SelectboxColumn(
                    "Review ✎", options=list(ls.REVIEW_STATUSES), required=True),
                "Comment": st.column_config.TextColumn("Comment ✎"),
            })
    if save:
        _save(data, records, base, edited, (reviewer or "").strip())


def _save(data: lk.ReleaseLeakage, records: dict[str, ls.Record], base: pd.DataFrame,
          edited: pd.DataFrame, reviewer: str) -> None:
    if not reviewer:
        st.warning("Add your name first — it is what the audit trail records.")
        return
    store, saved = ls.store(), 0
    for key in base.index:
        b, e, rec = base.loc[key], edited.loc[key], records[key]
        final = {"uat_detectability": e["Verdict"] or "", "category": e["Category"] or "",
                 "testrail_case": ", ".join(f"C{x}" for x in
                                            re.findall(r"\d+", e["TestRail case"] or ""))}
        changed = final != {k: rec.final.get(k, "") for k in ls.FINAL_FIELDS}
        status, comment = e["Review"], (e["Comment"] or "").strip()
        if changed and status in (ls.PENDING, ls.CONFIRMED):
            ai_final = {k: (rec.ai or {}).get(k, "") for k in ls.FINAL_FIELDS}
            status = ls.CONFIRMED if final == ai_final else ls.CHANGED
        if changed or status != b["Review"] or comment:
            store.review(data.group.project, key, reviewer, status, final, comment)
            saved += 1
    if saved:
        ver_key = f"lk_ver_{data.group.project}"
        st.session_state[ver_key] = st.session_state.get(ver_key, 0) + 1
        st.toast(f"Saved {saved} review{'s' if saved != 1 else ''}.", icon="💾")
        st.rerun(scope="fragment")
    else:
        st.info("Nothing to save: no value, status or comment was changed.")


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
        st.markdown(f"**{html.escape(row['summary'])}**")
        for field, label in (("description", "Description"), ("steps", "Steps to reproduce"),
                             ("actual", "Actual results"), ("expected", "Expected results")):
            with st.expander(label + ("" if row[field] else " — not filled")):
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
    if rec.events or rec.past_ai:
        st.markdown("**History**")
        st.dataframe(pd.DataFrame(lx.audit_rows({key: rec})).drop(columns="Incident"),
                     hide_index=True, width="stretch")


# ── insights ─────────────────────────────────────────────────────────────────
def _bars(counts: Counter, title: str) -> None:
    st.markdown(f"**{title}**")
    if not counts:
        st.caption("Nothing to show yet.")
        return
    df = pd.DataFrame(counts.most_common(), columns=["label", "n"])
    base = alt.Chart(df).encode(
        y=alt.Y("label:N", sort="-x", title=None,
                # labelOverlap off: with bars 26px apart Vega dropped every
                # other label as "overlapping", leaving bars nobody could name.
                axis=alt.Axis(labelLimit=260, labelColor=COLORS["text"], labelFontSize=12,
                              labelOverlap=False, domain=False, ticks=False)),
        x=alt.X("n:Q", title=None, axis=None),
        tooltip=[alt.Tooltip("label:N", title=title), alt.Tooltip("n:Q", title="Incidents")])
    chart = (base.mark_bar(color=COLORS["brand"], cornerRadiusTopRight=4,
                           cornerRadiusBottomRight=4, height=16)
             + base.mark_text(align="left", dx=4, color=COLORS["muted"]).encode(text="n:Q"))
    st.altair_chart(chart.properties(height=max(60, 26 * len(df))).configure_view(stroke=None),
                    width="stretch")


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


def _trend_chart(group: lk.Group, rels: list[lk.Release]) -> None:
    st.markdown("**Leakage ratio by release**")
    try:
        with st.spinner("Reading the last releases from Jira…"):
            history = lk.trend(group, rels, _today())
    except Exception as exc:
        logger.exception("Leakage: release history of %s unavailable", group.project)
        st.warning(f"The release history could not be read from Jira ({exc}).")
        return
    short = short_names([d.release.name for d in history])
    rows = [{"release": short[d.release.name]
             + (" *" if d.window and d.window.open_ended else ""),
             "name": d.release.name,
             "ratio": d.ratio * 100 if d.ratio is not None else None,
             "leaked": len(d.leaks), "uat": len(d.uat),
             "hh": _pct(d.ratio_hh),
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
    st.altair_chart((bars + limit).properties(height=230).configure_view(stroke=None),
                    width="stretch")
    st.caption(f"Dashed line: the {lk.LEAKAGE_THRESHOLD:.0%} limit.  * counted to date: "
               f"the next release has not shipped yet.")


def _insights(data: lk.ReleaseLeakage, rels: list[lk.Release], finals: dict[str, dict],
              gaps: dict[str, str], records: dict[str, ls.Record]) -> None:
    _trend_chart(data.group, rels)
    if not data.leaks:
        return
    by_key = {r["key"]: r for r in data.leaks}
    c1, c2 = st.columns(2, gap="large")
    with c1:
        _bars(Counter(f.get("uat_detectability") or "Not analysed" for f in finals.values()),
              "Verdict")
        _bars(Counter(g for g in gaps.values() if g != "—"), "Coverage gap (UAT-detectable only)")
    with c2:
        _bars(Counter(f.get("category") or "Not analysed" for f in finals.values()), "Category")
        _bars(Counter(c for r in data.leaks for c in (r["components"] or ["No component"])),
              "Jira component")
    areas = Counter((records[k].ai or {}).get("functional_area") for k in by_key)
    repeated = {a: n for a, n in areas.items() if a and n > 1}
    cases = Counter(c for f in finals.values()
                    for c in re.findall(r"C\d+", f.get("testrail_case") or ""))
    c3, c4 = st.columns(2, gap="large")
    with c3:
        st.markdown("**Functional areas with more than one incident**")
        if repeated:
            st.dataframe(pd.DataFrame(sorted(repeated.items(), key=lambda x: -x[1]),
                                      columns=["Area (AI)", "Incidents"]),
                         hide_index=True, width="stretch")
        else:
            st.caption("None in this release.")
    with c4:
        st.markdown("**TestRail cases tied to more than one incident**")
        multi = [(c, n) for c, n in cases.most_common() if n > 1]
        if multi:
            st.dataframe(pd.DataFrame(multi, columns=["Case", "Incidents"]),
                         hide_index=True, width="stretch")
        else:
            st.caption("None in this release.")


# ── the simple lists ─────────────────────────────────────────────────────────
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
    st.dataframe(df, hide_index=True, width="stretch", height=min(38 + 35 * len(df), 520),
                 column_config={"Issue": st.column_config.LinkColumn(
                     "Issue", display_text=r".*/browse/(.*)")})


@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def _all_bus(today) -> pd.DataFrame:
    rows = []
    for g in lk.GROUPS:
        try:
            rels = lk.releases(g)
            last = lk.latest_released(rels)
            if last is None:
                continue
            d = lk.analyse(g, rels, last.name, today)
        except Exception:
            logger.exception("Leakage summary failed for %s", g.label)
            rows.append({"Business Unit": g.label, "Latest release": "Jira unavailable"})
            continue
        rows.append({
            "Business Unit": g.label, "Latest release": last.name,
            "Released": last.date, "Window": "to date" if d.window.open_ended else "closed",
            "UAT issues": len(d.uat), "Leaked": len(d.leaks),
            "Leakage ratio": d.ratio * 100 if d.ratio is not None else None,
            "High & Highest": d.ratio_hh * 100 if d.ratio_hh is not None else None,
        })
    return pd.DataFrame(rows)


def _all_bus_view() -> None:
    with st.spinner("Reading every BU's latest release from Jira…"):
        df = _all_bus(_today())
    st.dataframe(df, hide_index=True, width="stretch", column_config={
        "Leakage ratio": st.column_config.NumberColumn(
            f"Leakage ratio (limit {lk.LEAKAGE_THRESHOLD:.0%})", format="%.1f%%"),
        "High & Highest": st.column_config.NumberColumn(
            f"High & Highest (limit {lk.LEAKAGE_HH_THRESHOLD:.0%})", format="%.1f%%")})
    st.caption("Each BU's latest shipped release; a window marked 'to date' is still open "
               "until the next release ships.")


# ── render ───────────────────────────────────────────────────────────────────
@st.fragment
def render() -> None:
    if not jira_client.available():
        st.info("Leakage reads releases and incidents from Jira — add JIRA_URL, "
                "ATLASSIAN_USER and ATLASSIAN_API_KEY to the app secrets.")
        return
    _scope, bu = global_filter.current()
    group = lk.group_for_bu(bu) if bu else None
    if group is None:
        st.info(f"No Jira project is mapped to **{bu}**: releases and incidents are "
                f"tracked per Jira project, and this Business Unit has none of its own. "
                f"Every BU's latest release is below.")
        _all_bus_view()
        return
    if group.shared:
        st.caption(f"{group.project} serves {', '.join(group.bus)}: its releases and "
                   f"incidents cannot be split per Business Unit, so they are shown together.")
    try:
        rels = lk.releases(group)
    except Exception as exc:
        logger.exception("Leakage: releases of %s unavailable", group.project)
        st.warning(f"The releases of {group.project} could not be read from Jira ({exc}).")
        return
    if not rels:
        st.info(f"No fixVersion of {group.project} matches its release stream "
                f"(`{group.release_pattern}`).")
        return
    release, c_export = _pick_release(group, rels)
    if release is None:
        return
    try:
        with st.spinner("Reading the release from Jira…"):
            data = lk.analyse(group, rels, release.name, _today())
    except Exception as exc:
        logger.exception("Leakage: %s %s unavailable", group.project, release.name)
        st.warning(f"The release could not be read from Jira ({exc}). The counts are not "
                   f"shown rather than shown as zero; try again in a few minutes.")
        return
    _window_caption(data)

    store = ls.store()
    index, ctx = None, {r["key"]: ([], []) for r in data.leaks}
    if data.leaks:
        try:
            index = lm.index_for(group.bus)
            ctx = _context(group.bus, _rows_json(data.leaks))
        except Exception:
            logger.exception("Leakage: TestRail matching unavailable")
            st.caption("TestRail cases are not loaded yet — matching will appear once "
                       "the dashboard data has finished loading.")
        _auto_ai(data, ctx)
    records = {r["key"]: store.get(group.project, r["key"]) for r in data.leaks}
    finals = {k: rec.final for k, rec in records.items()}
    gaps = {k: _gap(finals[k], records[k], ctx[k][0], index) for k in records}

    c_export.download_button(
        "⬇ Excel", data=lambda: lx.workbook(
            data, records, gaps, {"Exported at": ls.now(),
                                  "AI models": ", ".join(sorted({r.ai_model for r in records.values()
                                                                 if r.ai_model})),
                                  "Categories": "proposed, to validate with the QA team"}),
        file_name=f"leakage_{group.project}_{release.name}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"lk_xlsx_{group.project}", width="stretch")

    _cards(data, finals, gaps, records)
    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
    view = st.segmented_control("View", _VIEWS, default=_VIEWS[0], required=True,
                                key="lk_view", label_visibility="collapsed") or _VIEWS[0]
    if view == _VIEWS[0]:
        if not data.leaks:
            st.caption("No leaked incident to review in this window.")
        else:
            _ai_status(data, ctx, records)
            _review_table(data, records, gaps)
            _inspector(data, records, ctx, index)
    elif view == _VIEWS[1]:
        _insights(data, rels, finals, gaps, records)
    elif view == _VIEWS[2]:
        _issue_table(data.uat)
        if data.uat_left_out:
            st.markdown(f"**Not counted** · {len(data.uat_left_out)} Defect"
                        f"{'s' if len(data.uat_left_out) != 1 else ''} of the fixVersion "
                        f"created before the previous release, as Delivery counts it")
            _issue_table(data.uat_left_out, {"Why": "excluded_because"})
    elif view == _VIEWS[3]:
        _issue_table(data.excluded, {"Excluded because": "excluded_because",
                                     "Root cause (Jira)": "root_cause"})
    else:
        _all_bus_view()
