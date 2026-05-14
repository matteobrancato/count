from __future__ import annotations

import traceback

import streamlit as st

from src import testrail_client as tr
from src.ui import backlog_tab, overview_tab, pivot_tab, report_tab, theme


st.set_page_config(
    page_title="Automation Coverage",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# -------------------------------------------------------------------- header
def _header() -> None:
    c = theme.colors()
    left, mid, right = st.columns([4, 0.9, 1.1])
    with left:
        st.markdown(
            f"<h1 style='margin:0;padding:6px 0 2px;white-space:nowrap;color:{c['text']}'>"
            f"🧪 Automation Coverage</h1>"
            f"<div style='color:{c['text_2']};font-size:14px;padding-bottom:6px'>"
            f"Live view of TestRail's automation coverage across Business Units."
            f"</div>",
            unsafe_allow_html=True,
        )
    with mid:
        st.write("")
        theme.toggle_button()
    with right:
        st.write("")
        if st.button("🔄 Refresh Numbers", use_container_width=True,
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
    theme.inject_css()   # No-op in light mode; injects overrides in dark mode.
    _header()
    if not _creds_ok():
        st.stop()

    # Pre-fetch all suite data in the background on first load.
    # Uses a module-level flag so it runs only once per process.
    # After this completes, every BU click only needs Python processing.
    try:
        from src.rules_engine import warmup_cache
        with st.spinner("⚡ Pre-loading test suites…"):
            warmup_cache()
    except ImportError:
        pass

    tab_explore, tab_backlog, tab_overview, tab_report, tab_debug = st.tabs(
        ["📊 Explorer", "📋 Backlog", "🧭 Overview", "📄 Report", "Debug"]
    )

    try:
        with tab_explore:
            pivot_tab.render()
        with tab_backlog:
            backlog_tab.render()
        with tab_overview:
            overview_tab.render()
        with tab_report:
            report_tab.render()
        with tab_debug:
            from src.ui import debug_tab
            debug_tab.render()
    except Exception as exc:  # global safety net — never crash the whole app
        st.error(f"Unexpected error: {exc}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())


if __name__ == "__main__":
    main()
