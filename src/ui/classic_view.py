"""The previous layout, kept as the dashboard's last tab.

Until the Business Unit window, a BU was picked in a dropdown and its Detail,
Coverage and Leakage lived in tabs of their own beside the Backlog table.  This
view is that layout, built from the same sections, so the new one can be checked
against it number for number.  It is meant to go once the window has been
trusted: removing it is deleting this module and its line in `app.py`.

Only the open section runs, as everywhere (the tabs are lazy), and nothing here
fetches anything of its own.
"""
from __future__ import annotations

import streamlit as st

from . import backlog_tab, coverage_tab, global_filter, isolation, leakage_tab

# (label, renderer, animation container key) — the old tab bar, Trend and All BUs
# included.  They used to appear only beside Leakage; here they are always shown.
_SECTIONS = [
    ("📋 Backlog", lambda: backlog_tab.render(pick_bu=False), "backlog_anim"),
    ("🔎 Detail", backlog_tab.render_detail, "detail_anim"),
    ("📐 Coverage", coverage_tab.render, ""),
    ("🐞 Leakage", leakage_tab.render, ""),
    ("📈 Trend", leakage_tab.render_trend, ""),
    ("🌍 All BUs", leakage_tab.render_all_bus, ""),
]


def _follow(scope: str, key: str) -> None:
    """The dropdown moved: the sections read the BU from `global_filter`."""
    global_filter.select_bu(scope, st.session_state[key])


def render() -> None:
    scope, bu = global_filter.current()
    st.caption("The layout before the Business Unit window — same numbers, the "
               "Business Unit picked here. Kept for comparison; it will be removed.")
    if scope == "martech":
        # The old layout never had Martech: its table, as it was drawn, is all
        # there is to compare.
        backlog_tab.render(pick_bu=False)
        return
    # The dropdown always shows what the sections read: the BU opened in the
    # window is the one selected here too.
    key = f"classic_bu_{scope}"
    st.session_state[key] = bu
    st.selectbox("Business Unit", global_filter.bus_for_scope(scope), key=key,
                 on_change=_follow, args=(scope, key),
                 label_visibility="collapsed", width=220)
    tabs = st.tabs([label for label, _fn, _anim in _SECTIONS],
                   key="classic_section", on_change="rerun")
    opened = next((i for i, t in enumerate(tabs) if t.open), 0)
    label, render_fn, anim_key = _SECTIONS[opened]
    with tabs[opened]:
        isolation.render_isolated(render_fn, f"{label} (previous layout)", anim_key)
