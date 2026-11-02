"""Martech in the dashboard: the backlog of the Testim automation plan.

The table lists the Business Units with what the plan says about their tests;
a click on one opens its window (`bu_popup`), which breaks it down by run and by
status and lists the tests themselves.  The data and its rules are in `martech`;
this module only draws them.

Two things a reader must never be able to take for granted, and so are said on
the page rather than left to a methodology note: that this is the PLAN's view —
there is no label or field behind it — and that the progress bar is a share of a
backlog, not a coverage, which is why it is never coloured against the 80% target.
"""
from __future__ import annotations

import html
import io
import logging

import pandas as pd
import streamlit as st

from .. import freshness, martech
from . import bu_popup, global_filter
from .styles import COLORS, section_title, stat_card

logger = logging.getLogger(__name__)

# What the plan's own description says four of the statuses mean.
_LEGEND = ("**Passed** = merged · **Accepted fail** = PR raised · "
           "**Failed** = query raised to MQA · **N/A** = not applicable. "
           "Any other status is shown under the name TestRail gives it.")


def _load():
    """(read data, statuses, runs), or None after saying why there is nothing to
    show.  A failure is shown, never turned into zeros — a backlog of 0 would be
    a figure that lies."""
    try:
        with st.spinner("Reading the Martech backlog from TestRail…"):
            data = martech.fetch_plan()
            statuses = martech.fetch_statuses()
    except martech.MartechError as exc:
        logger.warning("Martech could not be read: %s", exc)
        st.error(f"Martech could not be read: {exc}")
        return None
    runs = martech.runs_in_plan(data["plan"])
    if not runs:
        st.info("The Martech plan has no run yet.")
        return None
    return data, statuses, runs


def _link(url: str, text: str) -> str:
    return (f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener">{html.escape(text)}</a>')


def _progress_cell(figures: dict[str, int], pct: float) -> str:
    if not figures["Total"] - figures["Not Applicable"]:
        return (f'<td class="l"><span class="cov-val" '
                f'style="color:{COLORS["muted"]}">—</span></td>')
    # Brand blue, never the RAG colours: the 80% target is a coverage target and
    # a backlog's progress is judged by nobody yet.
    return ('<td class="l"><div class="cov-wrap"><div class="cov-track">'
            f'<div class="cov-fill" style="width:{min(pct, 100):.0f}%;'
            f'background:{COLORS["brand"]}"></div></div>'
            f'<span class="cov-val" style="color:{COLORS["ink"]}">{pct:.1f}%</span>'
            '</div></td>')


def _num(row: pd.Series, col: str, strong: bool = False) -> str:
    return f'<td class="{"strong" if strong else "mut"}">{int(row[col]):,}</td>'


def _table_html(summary: pd.DataFrame, selected_bu: str, clickable: bool) -> str:
    fixed = {"BU", "Runs", "Total", "Merged", "Backlog", "Not Applicable", "Progress %"}
    status_cols = [c for c in summary.columns if c not in fixed]
    head = (
        '<thead><tr><th class="l">Business Unit</th><th>Total</th>'
        '<th>Merged<span class="th-sub">Passed</span></th>'
        '<th>Backlog<span class="th-sub">not merged, not N/A</span></th>'
        + "".join(f"<th>{html.escape(c)}</th>" for c in status_cols)
        + '<th>Not Applicable</th>'
        '<th class="l">Progress<span class="th-sub">merged of what is left</span></th>'
        '</tr></thead>')
    rows = []
    for _, r in summary.iterrows():
        bu = str(r["BU"])
        n_runs = int(r["Runs"])
        figures = {k: int(r[k]) for k in ("Total", "Merged", "Backlog", "Not Applicable")}

        sel_cls = " class='sel'" if selected_bu and bu == selected_bu else ""
        runs_note = f"{n_runs} run" if n_runs == 1 else f"{n_runs} runs"
        rows.append(
            f"<tr{sel_cls}{bu_popup.row_attr(bu, clickable)}>"
            + bu_popup.name_cell(bu, clickable, runs_note)
            + _num(r, "Total", True) + _num(r, "Merged", True) + _num(r, "Backlog", True)
            + "".join(_num(r, c) for c in status_cols)
            + _num(r, "Not Applicable")
            + _progress_cell(figures, float(r["Progress %"])) + "</tr>")
    return (f'<div class="bl-summary{" bl-clickable" if clickable else ""}"><table>{head}'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def render(*, pick_bu: bool = True) -> None:
    """The Backlog tab under the Martech scope."""
    scope, _bu = global_filter.current()
    # Read first: the table is drawn with the BU just clicked as the one opened.
    picked = bu_popup.clicked() if pick_bu else None
    if picked:
        global_filter.select_bu(scope, picked)
    loaded = _load()
    if loaded is None:
        return
    data, statuses, runs = loaded
    summary = martech.summary(runs, statuses, order=global_filter.bus_for_scope(scope))

    total = int(summary["Total"].sum())
    c_title, c_meta = st.columns([3, 4], vertical_alignment="center")
    with c_title:
        section_title("All Business Units", top=0)
        if pick_bu:
            st.caption("Click a Business Unit to see its runs, statuses and tests.")
    with c_meta, st.container(key="summary_export", horizontal=True,
                              vertical_alignment="center",
                              horizontal_alignment="right", gap="small"):
        st.markdown(
            f'<span style="font-size:12px;color:{COLORS["muted"]};white-space:nowrap">'
            f'{total:,} tests in {len(runs)} runs · read '
            f'{freshness.relative_time(data["read_at"])}</span>',
            unsafe_allow_html=True)
        st.download_button(
            "⬇ CSV", summary.to_csv(index=False).encode("utf-8"),
            file_name="martech_backlog.csv", mime="text/csv",
            help="Download the table above, as a CSV.")
    st.markdown(_table_html(summary, global_filter.opened(scope) if pick_bu
                            else "", pick_bu), unsafe_allow_html=True)
    plan = data["plan"]
    st.markdown(
        f'<span style="font-size:12px;color:{COLORS["muted"]}">Read from the plan '
        f'{_link(martech.plan_url(data["base_url"]), str(plan.get("name") or "plan"))}'
        f' — there is no label or field behind these numbers, only the plan.</span>',
        unsafe_allow_html=True)
    st.caption(_LEGEND)
    if picked:
        bu_popup.open_for(scope, picked)


def _xlsx(frame: pd.DataFrame, bu: str):
    """A deferred payload for `st.download_button`: the workbook is built only
    when the button is clicked, from a frame captured here."""
    def _build() -> bytes:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xl:
            frame.to_excel(xl, sheet_name=f"{bu}"[:31], index=False)
        return buf.getvalue()
    return _build


def render_bu(bu: str) -> None:
    """The window of one Martech BU: its figures, its runs and its tests."""
    loaded = _load()
    if loaded is None:
        return
    data, statuses, runs = loaded
    mine = [r for r in runs if r.bu == bu]
    if not mine:
        st.info(f"No run of **{bu}** is in the plan.")
        return
    f = martech.bu_figures(runs, statuses, bu)

    for col, name in zip(st.columns(4), ("Total", "Merged", "Backlog", "Not Applicable")):
        stat_card(col, name, f[name])
    st.caption(
        f"Progress {martech.progress(f):.1f}% — merged of the tests not marked N/A. "
        "Merged is TestRail's Passed; Backlog is every other status except N/A.")

    open_ids = martech.open_statuses(mine, statuses)
    if open_ids:
        section_title("Backlog by status", top=10)
        for chunk_start in range(0, len(open_ids), 4):
            ids = open_ids[chunk_start:chunk_start + 4]
            for col, sid in zip(st.columns(4), ids):
                stat_card(col, martech.label(statuses, sid),
                          sum(r.counts.get(sid, 0) for r in mine))

    section_title("By run", top=10)
    st.dataframe(martech.runs_summary(runs, statuses, bu), hide_index=True,
                 width="stretch")

    section_title("Tests", top=10)
    tests: dict[int, list[dict]] = {}
    try:
        with st.spinner(f"Reading the {f['Total']:,} tests from TestRail…"):
            for r in mine:
                tests[r.run_id] = martech.fetch_run_tests(r.run_id)
    except martech.MartechError as exc:
        logger.warning("Martech tests of %s could not be read: %s", bu, exc)
        st.warning(f"The tests could not be listed: {exc} The counts above are "
                   f"unaffected.")
        return
    frame = martech.tests_frame(mine, tests, statuses, data["base_url"])
    if len(frame) != f["Total"]:
        # Counts and list are cached separately, so after a refresh half-way
        # through one of them they can differ by what moved in between.
        st.caption(f"The list holds {len(frame):,} tests, the counts {f['Total']:,}: "
                   f"they were read at different times (↻ reads both again).")
    st.caption(f"{frame['Case'].nunique():,} distinct cases — one case in two runs is "
               f"two tests.")
    present = sorted(frame["Status"].unique())
    picked = st.multiselect("Status", present, default=present, key=f"mt_status_{bu}",
                            label_visibility="collapsed", placeholder="Status")
    shown = frame[frame["Status"].isin(picked)]
    st.dataframe(shown, hide_index=True, width="stretch", column_config={
        "Case": st.column_config.LinkColumn("Case", display_text=r".*/view/(\d+)"),
        "Title": st.column_config.TextColumn("Title", width="large")})
    st.download_button(
        f"⬇ Excel · {len(shown):,} tests", _xlsx(shown, bu),
        file_name=f"martech_{bu.replace(' ', '_')}_tests.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=f"mt_dl_{bu}", help="The tests listed above, exactly as shown.")
