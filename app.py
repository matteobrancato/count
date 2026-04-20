from __future__ import annotations

import traceback

import streamlit as st

from src import testrail_client as tr
from src.ui import overview_tab, pivot_tab


st.set_page_config(
    page_title="Automation Coverage",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# -------------------------------------------------------------------- header
def _header() -> None:
    left, right = st.columns([4, 1])
    with left:
        st.markdown(
            "<h1 style='margin:0'>🧪 Automation Coverage</h1>"
            "<div style='color:#5e6677;font-size:14px'>"
            "Live view of TestRail's automation coverage across Business Units."
            "</div>",
            unsafe_allow_html=True,
        )
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

    tab_explore, tab_overview, tab_debug = st.tabs(
        ["📊 Explorer", "🧭 Overview", "Debug"]
    )

    try:
        with tab_explore:
            pivot_tab.render()
        with tab_overview:
            overview_tab.render()
        with tab_debug:
            from src.ui import debug_tab
            debug_tab.render()
    except Exception as exc:  # global safety net — never crash the whole app
        st.error(f"Unexpected error: {exc}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())


if __name__ == "__main__":
    main()
