from __future__ import annotations

import time
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
@st.cache_data(ttl=3600, show_spinner=False)
def _numbers_fetched_at() -> float:
    """Wall-clock time the current cached numbers were fetched.

    Cached cross-session with the SAME ttl as `evaluate_rules`, so it represents
    the real age of the data (not when *this* browser tab opened) and survives
    page reloads.  Cleared by "Refresh Numbers" alongside the data caches, so it
    resets to 'now' on a manual refresh.
    """
    return time.time()


def _relative_time(ts: float) -> str:
    """Human 'time ago' for the data-freshness caption."""
    delta = max(0.0, time.time() - ts)
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{round(delta / 60)}m ago"
    if delta < 86400:
        return f"{round(delta / 3600)}h ago"
    return f"{round(delta / 86400)}d ago"


def _header() -> None:
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


def _freshness_label() -> None:
    """Small data-freshness caption, CSS-pinned (`.st-key-freshness`) to the
    top-right of the tab bar — level with the tabs.  Purely informational: there
    is no manual refresh; the numbers auto-refresh hourly via the cache ttl."""
    updated_at = _numbers_fetched_at()
    with st.container(key="freshness"):
        st.markdown(
            f"<div style='color:{COLORS['muted']};font-size:11px;"
            f"white-space:nowrap;line-height:1'>Updated "
            f"<b style='color:{COLORS['text']};font-weight:600'>"
            f"{_relative_time(updated_at)}</b></div>",
            unsafe_allow_html=True,
        )


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

    # Wrap the tab bar in a relative-positioned zone so the freshness label can
    # be pinned to its top-right (= the tab row), reliably level with the tabs.
    with st.container(key="tabs_zone"):
        _freshness_label()
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
