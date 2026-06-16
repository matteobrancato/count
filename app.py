from __future__ import annotations

import traceback

import streamlit as st

from src import testrail_client as tr
from src.ui import (
    backlog_tab, chat_assistant, coverage_tab, overview_tab,
    pivot_tab, report_tab, runs_tab, styles,
)
from src.ui.styles import COLORS


st.set_page_config(
    page_title="Automation Coverage",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# -------------------------------------------------------------------- header
def _header() -> None:
    left, right = st.columns([4, 1], vertical_alignment="center")
    with left:
        st.markdown(
            f"<div style='display:flex;align-items:center;gap:14px'>"
            f"<div style='width:46px;height:46px;border-radius:13px;flex:0 0 auto;"
            f"display:flex;align-items:center;justify-content:center;font-size:24px;"
            f"background:linear-gradient(135deg,{COLORS['brand']} 0%,{COLORS['brand_strong']} 100%);"
            f"box-shadow:0 4px 14px rgba(46,91,255,0.30)'>🧪</div>"
            f"<div>"
            f"<h1 style='margin:0;padding:0;line-height:1.05;white-space:nowrap;"
            f"font-size:30px'>Automation Coverage</h1>"
            f"<div style='color:{COLORS['muted']};font-size:13.5px;margin-top:3px'>"
            f"Live view of TestRail&rsquo;s automation coverage across Business Units."
            f"</div></div></div>",
            unsafe_allow_html=True,
        )
    with right:
        if st.button("🔄  Refresh Numbers", use_container_width=True,
                     key="refresh_numbers", type="primary",
                     help="Clear all caches and re-fetch from TestRail."):
            tr.clear_all_caches()
            try:
                from src.rules_engine import evaluate_rules
                evaluate_rules.clear()
            except Exception:
                pass
            st.rerun()


# -------------------------------------------------------------------- credentials gate
def _creds_ok() -> bool:
    try:
        tr.TestRailCredentials.from_secrets()
        return True
    except tr.TestRailError as exc:
        st.error(str(exc))
        st.code(
            '# .streamlit/secrets.toml\n'
            'TESTRAIL_URL = "https://elabaswatson.testrail.io"\n'
            'TESTRAIL_USER = "your.email@example.com"\n'
            'TESTRAIL_API_KEY = "your_api_key"',
            language="toml",
        )
        return False


# -------------------------------------------------------------------- main
def main() -> None:
    styles.inject()   # global design system — purely cosmetic, must run first.
    _header()
    if not _creds_ok():
        st.stop()

    # Render the floating chat FIRST — Streamlit renders incrementally, so
    # placing it here makes the FAB appear immediately, before the (slow)
    # warmup_cache and the eager tab renders below.  `position: fixed` in the
    # CSS handles the visual placement, so DOM order doesn't matter.
    try:
        chat_assistant.render_floating_button()
    except Exception:  # noqa: BLE001 — never let the chat break the app
        traceback.print_exc()

    # Pre-fetch all suite data in the background on first load.
    # Uses a module-level flag so it runs only once per process.
    # After this completes, every BU click only needs Python processing.
    try:
        from src.rules_engine import warmup_cache
        with st.spinner("⚡ Pre-loading test suites…"):
            warmup_cache()
    except ImportError:
        pass

    (tab_explore, tab_backlog, tab_coverage, tab_overview, tab_report,
     tab_runs, tab_debug) = st.tabs(
        ["📊 Explorer", "📋 Backlog", "📐 Coverage", "🧭 Overview",
         "📄 Report", "🏃 Runs", "Debug"]
    )

    try:
        with tab_explore:
            pivot_tab.render()
        with tab_backlog:
            backlog_tab.render()
        with tab_coverage:
            coverage_tab.render()
        with tab_overview:
            overview_tab.render()
        with tab_report:
            report_tab.render()
        with tab_runs:
            runs_tab.render()
        with tab_debug:
            from src.ui import debug_tab
            debug_tab.render()
    except Exception as exc:  # global safety net — never crash the whole app
        st.error(f"Unexpected error: {exc}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())


if __name__ == "__main__":
    main()
