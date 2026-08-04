"""Debug tab — a clone of Backlog used to try a different structure safely.

The live Backlog tab stacks three populations down one page: the big_regr
baseline, a Production Sanity block, and (with Small NR) a third would follow.
Each arrived as its own row of tiles, so the page grew a section every time the
business gained a run, and a reader met three sets of "Total / Automated /
Backlog" before reaching the pivot.

The experiment here: the three are RUNS, which is how the spreadsheet this
dashboard replaces already talks about them ("Full regression run", "Release
regression run", "Prod Sanity run").  So one control picks the run and the whole
tab — summary table, tiles, coverage, frameworks, pivot — reports on that one.
One story at a time instead of three stacked.

Nothing is duplicated: every number comes from the Backlog tab's own functions,
so the two tabs cannot drift while the experiment runs.

  Big No-Regression    every big_regr row            (today's default)
  Small No-Regression  the `small_nr` SUBSET of the same rows
  Production Sanity    the separate `prod_sanity` baseline
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from . import backlog_tab as bl
from . import global_filter
from .styles import section_title

# Plain text: the glyph rendered clipped inside the button, and it was
# decoration — the three names already say which run you are looking at.
_RUN_BIG   = "Big No-Regression"
_RUN_SMALL = "Small No-Regression"
_RUN_PS    = "Production Sanity"
_RUNS = [_RUN_BIG, _RUN_SMALL, _RUN_PS]


def _small_nr_cases(scope: str) -> set[int]:
    """Case IDs carrying the `small_nr` checkbox.

    A SUBSET marker: these cases are already in the big_regr baseline, so this
    never adds rows — it only narrows the ones already counted.
    """
    try:
        raw, _auto, _rules = bl._load_scope(scope)
    except Exception:                                                   # noqa: BLE001
        return set()
    if raw.empty or "small_nr" not in raw.columns:
        return set()
    return set(raw.loc[raw["small_nr"].fillna(False), "case_id"].astype(int))


def _run_data(run: str, scope: str):
    """(summary, expanded_by_bu, auto_by_bu) for the selected run.

    Small NR reuses the regression payload and filters it, rather than expanding
    a second time: the subset shares every row with the baseline, so recomputing
    it could only produce the same rows more slowly — or, worse, differently.
    """
    if run == _RUN_PS:
        return bl._prod_sanity_data()
    summary, expanded_by_bu, auto_by_bu = (
        bl._mapp_backlog_data() if scope == "mobile_app" else bl._backlog_data())
    if run != _RUN_SMALL:
        return summary, expanded_by_bu, auto_by_bu

    ids = _small_nr_cases(scope)
    if not ids:
        return pd.DataFrame(), {}, {}
    exp_small = {
        key: exp[exp["case_id"].astype(int).isin(ids)]
        for key, exp in expanded_by_bu.items()
    }
    rows = []
    for (bu, sc), exp in exp_small.items():
        if exp.empty:
            continue
        s = bl._stats(exp, auto_by_bu.get((bu, sc), pd.DataFrame(
            columns=bl._AUTO_SLIM_COLS)))
        rows.append({
            "BU": bu, "Scope": bl._SCOPE_DISPLAY.get(sc, "Website"),
            "Total": s["total"], "Automated": s["automated"],
            "Backlog": s["backlog"],
            "Partially Automated": s["partially_automated"],
            "To be Updated": s["to_be_updated"],
            "Not Applicable": s["not_applicable"], "Unknown": s["unknown"],
            "Coverage %": round(s["cov_total"], 1),
            "Coverage excl. Partially %": round(s["cov_ex_partial"], 1),
        })
    return pd.DataFrame(rows), exp_small, auto_by_bu


@st.fragment
def render() -> None:
    scope, bu = global_filter.current()

    run = st.segmented_control(
        "Run", _RUNS, default=_RUN_BIG, required=True,
        key=f"debug_run_{scope}", label_visibility="collapsed",
    ) or _RUN_BIG

    with st.spinner("Computing…"):
        summary, expanded_by_bu, auto_by_bu = _run_data(run, scope)

    if summary is None or summary.empty:
        st.info(
            "Nothing in this run yet. Small No-Regression needs the "
            "`small_nr` checkbox in TestRail; Production Sanity needs the "
            "`prod_sanity` label. New values appear at the next data refresh "
            "(↻ next to the tabs)."
        )
        return

    scope_display = bl._SCOPE_DISPLAY.get(scope, "Website")
    display = summary[summary["Scope"] == scope_display]
    if display.empty:
        display = summary
    for empty_col in ("Unknown", "Playwright"):
        if empty_col in display.columns and int(display[empty_col].sum()) == 0:
            display = display.drop(columns=[empty_col])
    num_cols = [c for c in ["Total", "Automated", "Java", "TestIM", "Playwright",
                            "Backlog", "Partially Automated", "To be Updated",
                            "Not Applicable", "Unknown"]
                if c in display.columns]

    section_title("All Business Units", top=0)
    st.markdown(bl._summary_table_html(display, num_cols, selected_bu=bu),
                unsafe_allow_html=True)
    st.divider()

    section_title("Detail by Business Unit")
    exp = expanded_by_bu.get((bu, scope))
    if exp is None or exp.empty:
        st.info(f"No {run.split(' ', 1)[-1]} rows for **{bu}**.")
        return
    auto = auto_by_bu.get((bu, scope), pd.DataFrame(columns=bl._AUTO_SLIM_COLS))
    s = bl._stats(exp, auto)

    tiles = [
        ("Total", s["total"], s["u_total"], ""),
        ("Automated", s["automated"], s["u_auto"], ""),
        ("Backlog", s["backlog"], s["u_back"],
         bl._backlog_badge_html(s["backlog"], s["total"])),
        ("Partially Automated", s["partially_automated"], s["u_part"], ""),
        ("To be Updated", s["to_be_updated"], s["u_tbu"], ""),
        ("Not Applicable", s["not_applicable"], s["u_na"], ""),
    ]
    for col, (label, n, u, badge) in zip(st.columns(6), tiles):
        bl._stat_card(col, label, n, u, badge_html=badge)

    parts = [f"**Coverage:** `{s['cov_total']:.1f}%`",
             f"**Coverage vs Automatable:** `{s['cov_automatable']:.1f}%`"]
    if s["partially_automated"]:
        parts.append("**Coverage excluding Partially Automated:** "
                     f"`{s['cov_ex_partial']:.1f}%`")
    parts.append(f"**Not Applicable:** `{s['na_pct']:.1f}%`")
    st.markdown(" &nbsp;·&nbsp; ".join(parts), unsafe_allow_html=True)

    frameworks = bl._framework_cards(s)
    if frameworks:
        st.divider()
        section_title("Automated by Framework")
        cols = st.columns(max(3, len(frameworks)))
        for col, (name, n, u) in zip(cols, frameworks):
            bl._stat_card(col, name, n, u)

    st.divider()
    bl._baseline_pivot(exp, key_prefix=f"dbg_{bu}_{scope}_{_RUNS.index(run)}")
