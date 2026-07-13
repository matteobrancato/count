from __future__ import annotations

import time
import traceback
import streamlit as st
from src import testrail_client as tr
from src.ui import (
    backlog_tab, chat_assistant, coverage_tab, global_filter, kpi_strip,
    overview_tab, pivot_tab, report_tab, runs_tab, styles,
)
from src.ui.styles import COLORS


st.set_page_config(
    page_title="Automation Dashboard",
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
    """Data-freshness caption pinned (`.st-key-freshness`) to the tab-bar's
    top-right.  Hovering it reveals a tiny ↻ button (CSS animation) that
    refreshes ONLY the numbers: clears the data caches and reruns — the page
    chrome stays, the loader shows on the data."""
    updated_at = _numbers_fetched_at()
    with st.container(key="freshness"):
        st.markdown(
            f"<div style='color:{COLORS['muted']};font-size:11px;"
            f"white-space:nowrap;line-height:1'>Updated "
            f"<b style='color:{COLORS['text']};font-weight:600'>"
            f"{_relative_time(updated_at)}</b></div>",
            unsafe_allow_html=True,
        )
        # No help tooltip: it rendered a large card covering the label.  The ↻
        # glyph + hover rotation are self-explanatory.
        if st.button("↻", key="refresh_mini"):
            tr.clear_all_caches()
            try:
                from src.rules_engine import evaluate_rules
                evaluate_rules.clear()
            except Exception:
                pass
            try:
                from src.ui.backlog_tab import _backlog_data
                _backlog_data.clear()
            except Exception:
                pass
            try:
                from src.ui.chat_assistant import _build_coverage_brief
                _build_coverage_brief.clear()
            except Exception:
                pass
            try:
                from src.ui.kpi_strip import _kpis
                _kpis.clear()
            except Exception:
                pass
            _numbers_fetched_at.clear()
            tr._WARMED_AT = 0.0                       # re-run the parallel pre-warm
            st.session_state["_warmed_ui"] = False    # show the verbose status
            st.session_state["_kpi_filled"] = False   # re-swap skeleton -> strip
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
    # placing it here makes the FAB appear immediately, before the (slow) data
    # fetches in the tab renders below.  `position: fixed` in the CSS handles the
    # visual placement, so DOM order doesn't matter.
    try:
        chat_assistant.render_floating_button()
    except Exception:  # noqa: BLE001 — never let the chat break the app
        traceback.print_exc()

    # NOTE on load UX: we create the tab bar FIRST (instant skeleton), then warm
    # the whole cache inside the active tab below (not in a blocking pre-fetch
    # before st.tabs(), which used to leave the tab area blank/white).  So the
    # page chrome is visible immediately, the loader sits on the data area, and
    # every tab is pre-loaded — switching tabs stays instant.

    # Group KPI strip — an st.empty slot directly under the header.  Warm runs
    # fill it immediately; on a cold start a same-size shimmering skeleton holds
    # the space and is REPLACED after the warm-up, so the layout never shifts
    # (inserting content above already-rendered elements mid-run is what made
    # the strip visually merge with the filter bar).
    kpi_slot = st.empty()
    try:
        if st.session_state.get("_warmed_ui"):
            with kpi_slot.container():
                kpi_strip.render()
        else:
            with kpi_slot.container():
                kpi_strip.render_skeleton()
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    # Global scope + BU selector — the single control bar every tab reads from
    # (detail views follow it; all-BU overviews intentionally ignore the BU).
    global_filter.render()

    # Wrap the tab bar in a relative-positioned zone so the freshness label can
    # be pinned to its top-right (= the tab row), reliably level with the tabs.
    with st.container(key="tabs_zone"):
        _freshness_label()
        (tab_backlog, tab_coverage, tab_explore, tab_runs, tab_overview,
         tab_report) = st.tabs(
            ["📋 Backlog", "📐 Coverage", "📊 Explorer", "🏃 Runs",
             "🧭 Overview", "📄 Report"]
        )

    try:
        with tab_backlog:
            # Pre-load every suite ONCE, up-front, so switching tabs is instant
            # afterwards.  This sits in the FIRST (default-active) tab, so the
            # tab-bar skeleton is already on screen and the loader shows here in
            # the active tab — the page is never blank, yet we still warm
            # everything (not lazy-per-tab).  On the first load we show a verbose
            # step-by-step status (so the wait feels shorter); once warm, the
            # call is instant cache hits so we skip the UI entirely.
            # A warm-up failure must never blank the tab: worst case the tabs
            # fetch their own data lazily (each surfacing its own error).
            try:
                from src.rules_engine import warmup_cache
                if st.session_state.get("_warmed_ui"):
                    warmup_cache()
                else:
                    # The status lives in an st.empty slot: it streams the
                    # verbose steps WHILE loading, then is REMOVED from the DOM
                    # and replaced by a transient toast.  (The previous
                    # CSS-hide approach left the box in the DOM, and switching
                    # tabs re-triggered its animation — "Dashboard ready" kept
                    # reappearing.)
                    _warm_slot = st.empty()
                    _t0 = time.time()
                    with _warm_slot.container():
                        with st.container(key="warmup_status"):
                            with st.status("⚡ Loading dashboard data…",
                                           expanded=True) as _status:
                                warmup_cache(
                                    on_step=_status.write,
                                    on_label=lambda lbl: _status.update(label=lbl),
                                )
                                _status.update(label="✅ Dashboard ready",
                                               state="complete", expanded=False)
                    _warm_slot.empty()                       # gone for good
                    _elapsed = time.time() - _t0
                    st.toast(f"Dashboard ready — loaded in {_elapsed:.0f}s",
                             icon="✅")
                    st.session_state["_warmed_ui"] = True
            except ImportError:
                pass
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                st.warning(
                    "⚠️ Part of the data pre-load failed — sections will load "
                    "lazily and may be slower on first view."
                )
            # Cold start: swap the skeleton for the real strip now that the
            # data is warm — best-effort, the strip hides itself on failure.
            if not st.session_state.get("_kpi_filled"):
                try:
                    with kpi_slot.container():
                        kpi_strip.render()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
            st.session_state["_kpi_filled"] = True

            # `*_anim` containers opt each tab into the scroll-reveal animation
            # (styles.py) — Coverage wraps itself internally.
            with st.container(key="backlog_anim"):
                backlog_tab.render()
        with tab_coverage:
            coverage_tab.render()
        with tab_explore:
            with st.container(key="explorer_anim"):
                pivot_tab.render()
        with tab_overview:
            with st.container(key="overview_anim"):
                overview_tab.render()
        with tab_report:
            with st.container(key="report_anim"):
                report_tab.render()
        # Runs is rendered LAST on purpose (its position in the tab bar is
        # unchanged — content binds to its tab regardless of execution order):
        # on the first visit of a BU it fires 30-50s of TestRail calls (plan
        # details, failed results, stability tests), and executing it last
        # means every other tab is ready in seconds instead of queueing
        # behind it.
        with tab_runs:
            with st.container(key="runs_anim"):
                runs_tab.render()
    except Exception as exc:  # global safety net — never crash the whole app
        st.error(f"Unexpected error: {exc}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())


if __name__ == "__main__":
    main()
