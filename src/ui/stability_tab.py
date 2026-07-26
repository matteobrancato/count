"""Stability tab — how dependable the tests are, and the full story of one case.

Split out of the Runs tab, which had grown to four stacked sections: a reader
looking for flaky tests had to scroll past the active runs and the release
readiness card to reach them, and the page read as one long, undifferentiated
list.

The two tabs share ONE background warm-up (`runs_tab.live_context`), so opening
this tab never starts a second TestRail fetch — it waits on the same load the
Runs tab does and then reads the same caches.  The section renderers stay in
`runs_tab` next to the TestRail helpers they use (run collection, stability
classification, status maps); this module owns the tab's composition only.
"""
from __future__ import annotations

import streamlit as st

from . import runs_tab


@st.fragment
def render() -> None:
    st.caption(
        "How reliably the tests pass across recent completed runs, plus the "
        "full execution and bug history of any single test case."
    )
    ctx = runs_tab.live_context("stability")
    if ctx is None:
        # The deep dive is driven by a case ID, not by the live run data, so it
        # stays usable while the rest is still loading (or has failed).
        st.divider()
        runs_tab.render_case_deep_dive()
        return

    _scope, bu, project_ids, _base_url = ctx
    runs_tab.render_stability(bu, project_ids)
    st.divider()
    runs_tab.render_case_deep_dive()
