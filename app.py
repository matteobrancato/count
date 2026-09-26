from __future__ import annotations

import logging
import time
import traceback

import streamlit as st

from src import freshness
from src import testrail_client as tr
from src.methodology import METHODOLOGY_MD
from src.ui import (
    backlog_tab,
    chat_assistant,
    coverage_tab,
    data_quality,
    global_filter,
    kpi_strip,
    leakage_tab,
    overview_tab,
    report_tab,
    styles,
    test_design_tab,
)
from src.ui.styles import COLORS

logger = logging.getLogger(__name__)


st.set_page_config(
    page_title="Automation Dashboard",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# -------------------------------------------------------------------- freshness
# The numbers load once per business day and then stay cached all day — see
# src/freshness.py.  A session counts as warm only for the stamp it warmed up
# on, so a clear (the daily rollover, or ↻ pressed by anyone) sends EVERY open
# session back through the visible loader, instead of leaving a tab opened
# yesterday to reload silently behind a page that looks frozen.
def _is_warm() -> bool:
    return st.session_state.get("_warmed_for") == freshness.stamp()


def _mark_warm() -> None:
    st.session_state["_warmed_for"] = freshness.stamp()


# -------------------------------------------------------------------- header
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
    # Compact header: the page already stacks a KPI band + a filter band before
    # any content, so the title stays tight to leave the data above the fold.
    st.markdown(
        f"<div style='display:flex;align-items:center;gap:12px;margin-bottom:2px'>"
        f"<div style='width:38px;height:38px;border-radius:11px;flex:0 0 auto;"
        f"display:flex;align-items:center;justify-content:center;font-size:20px;"
        f"background:linear-gradient(135deg,{COLORS['brand']} 0%,{COLORS['brand_strong']} 100%);"
        f"box-shadow:0 3px 10px rgba(46,91,255,0.28)'>🧪</div>"
        f"<div>"
        f"<h1 style='margin:0;padding:0;line-height:1.05;white-space:nowrap;"
        f"font-size:24px'>Automation Coverage</h1>"
        f"<div style='color:{COLORS['muted']};font-size:12px;margin-top:1px'>"
        f"Live view of TestRail&rsquo;s automation coverage across Business Units."
        f"</div></div></div>",
        unsafe_allow_html=True,
    )


def _freshness_label(scope: str = "website") -> None:
    """Utility bar pinned (`.st-key-freshness`) to the tab-bar's top-right:

        ℹ️ How numbers are calculated · 🧹 Data quality · Updated 3m ago ↻

    The two disclosures are popovers styled as plain text links (underline on
    hover) so they carry no visual weight until needed — they used to be
    full-width expanders competing with the KPI strip.  Hovering the bar reveals
    the tiny ↻ that refreshes ONLY the numbers.
    """
    updated_at = freshness.stamp()
    # Native horizontal flex row (Streamlit ≥1.46): the items are centred on one
    # optical line by the framework, so no hand-rolled flex/line-height CSS is
    # needed — that was what left the labels sitting at different heights.
    with st.container(
        key="freshness", horizontal=True, vertical_alignment="center",
        gap="small", width="content",
    ):
        with st.popover("ℹ️ How numbers are calculated"):
            # Marker class — see `:has(.methodology-panel)` in styles.py.
            st.markdown('<div class="methodology-panel"></div>',
                        unsafe_allow_html=True)
            st.markdown(METHODOLOGY_MD)

        # The scan derives from already-loaded frames, but on a cold start those
        # frames don't exist yet — computing here would run the heavy load
        # BEFORE the warm-up status box.  So it only engages once warm.
        warm = _is_warm()
        n_findings = data_quality.finding_count(scope) if warm else None
        dq_label = ("🧹 Data quality" if not n_findings
                    else f"🧹 Data quality · {n_findings}")
        with st.popover(dq_label):
            if warm:
                data_quality.render_body(scope)
            else:
                st.caption("Available once the data has finished loading.")

        # ONE markdown element, not two.  "Updated 3m ago" and "· 4 workers"
        # used to be separate st.markdown calls, which made them separate flex
        # children: the gap before the "·" came from the container's `gap`
        # while the gap after it was a plain space, so the separator sat off
        # centre and each half carried its own line box.  A single span gives
        # the whole label one baseline and one symmetric separator.
        #
        # <span>, not <div>: Streamlit gives the markdown container a
        # margin-bottom of -1rem assuming a <p> inside supplies +1rem.  A bare
        # block <div> gets no such margin, so the box collapses 16px and the
        # text drifts below the row's centre line.  Inline HTML is wrapped in a
        # <p> by the markdown pass, which restores the balance.
        #
        # The worker count is appended only when there is something to say: it
        # is the difference between a load that is slow and a load that is slow
        # because something is misconfigured, and without it the pool is
        # invisible.
        try:
            _workers, _configured = tr.n_workers(), tr.n_accounts_configured()
            _rate = tr.rate_summary()
            _cap, _learned = int(_rate["limit_per_account"]), bool(_rate["learned"])
        except Exception:                                               # noqa: BLE001
            # The label is a convenience; losing it must not cost the bar.
            logger.exception("Worker count unavailable for the freshness bar")
            _workers = _configured = _cap = 0
            _learned = False
        _short = _configured > _workers
        # A figure TestRail stated in a 429 is a fact; the rate being tried
        # because TestRail has not refused it yet is only a lower bound.  The
        # tooltip says which one it is showing.
        _cap_words = (f"which TestRail currently limits to {_cap} requests/minute"
                      if _learned else
                      f"paced at up to {_cap} requests/minute, which TestRail "
                      f"has not refused")
        _parts = [
            f"<span style='color:{COLORS['muted']}'>Updated </span>"
            f"<b style='color:{COLORS['text']};font-weight:600'>"
            f"{_relative_time(updated_at)}</b>"
        ]
        if _workers > 1 or (_configured and _short):
            _tip = (
                f"Requests are spread across {_workers} TestRail account(s), "
                f"each with its own rate limit ({_cap_words}) — so the data "
                f"loads about {_workers}x faster than with one."
                + (f"  {_configured - _workers} configured account(s) are NOT "
                   f"answering and were left out; the app log names them."
                   if _short else "")
            )
            _parts.append(
                f"<span title='{_tip}' style='cursor:help;color:{COLORS['muted']}'>"
                f"<b style='color:{'#DC2626' if _short else COLORS['text']};"
                f"font-weight:600'>{_workers}</b>"
                + (f" of {_configured}" if _short else "")
                + " workers</span>"
            )
        _sep = (f"<span style='color:{COLORS['muted']};opacity:.55;"
                f"margin:0 6px'>·</span>")
        st.markdown(
            f"<span style='font-size:11px;white-space:nowrap'>"
            f"{_sep.join(_parts)}</span>",
            unsafe_allow_html=True,
        )
        # No help tooltip: it rendered a large card covering the label.  The ↻
        # glyph + hover rotation are self-explanatory.
        if st.button("↻", key="refresh_mini"):
            freshness.clear_everything()
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


def _render_isolated(render_fn, label: str, anim_key: str = "") -> None:
    """Run one section's renderer so that its failure stays in its own place.

    All tabs execute in the same script run, so one shared try/except meant a
    single failing tab blanked every tab after it.  Here each section catches
    its own failure, logs it with the traceback, and shows it where it
    happened — the rest of the dashboard stays usable.
    """
    try:
        if anim_key:
            with st.container(key=anim_key):
                render_fn()
        else:
            render_fn()
    except Exception as exc:  # noqa: BLE001 — isolate, never cascade
        logger.exception("%s failed to render", label)
        st.error(f"⚠️ {label} could not be rendered: {exc}")
        with st.expander("Technical details"):
            st.code(traceback.format_exc())


def _render_tab(tab, render_fn, label: str, anim_key: str = "") -> None:
    with tab:
        _render_isolated(render_fn, label, anim_key)


# -------------------------------------------------------------------- main
def main() -> None:
    styles.inject()   # global design system — purely cosmetic, must run first.
    _header()
    if not _creds_ok():
        st.stop()

    # The first run of a new business day clears yesterday's numbers; every
    # other run of the day is a cache hit.  Decided before anything is drawn,
    # so the freshness bar, the KPI strip and the loader all agree on it.
    try:
        freshness.roll_over_if_new_day()
    except Exception:  # noqa: BLE001 — yesterday's numbers beat a blank page
        logger.exception("Daily rollover failed; serving the cached numbers")
    cold = not _is_warm()

    # Build the account pool BEFORE anything reports on it.  It was built
    # lazily by the first fetch — which happens inside the tab, well after the
    # freshness bar has already been drawn — so on a cold start the bar showed
    # no workers and the right number only appeared on the next rerun.  That is
    # the whole of "cambia solo dopo refresh".  One parallel, unpaced round
    # trip, so the cost is a fraction of a second.
    try:
        tr.ensure_pool()
    except Exception:  # noqa: BLE001 — the fetches surface credential errors
        logger.exception("Could not build the TestRail account pool")

    # Render the floating chat FIRST — Streamlit renders incrementally, so
    # placing it here makes the FAB appear immediately, before the (slow) data
    # fetches in the tab renders below.  `position: fixed` in the CSS handles the
    # visual placement, so DOM order doesn't matter.
    try:
        chat_assistant.render_floating_button()
    except Exception:  # noqa: BLE001 — never let the chat break the app
        logger.exception("Dexter's button failed to render")

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
        with kpi_slot.container():
            if cold:
                kpi_strip.render_skeleton()
            else:
                kpi_strip.render()
    except Exception:  # noqa: BLE001
        logger.exception("KPI strip failed to render")

    # Global scope + BU selector — the single control bar every tab reads from
    # (detail views follow it; all-BU overviews intentionally ignore the BU).
    global_filter.render()

    # Wrap the tab bar in a relative-positioned zone so the freshness label can
    # be pinned to its top-right (= the tab row), reliably level with the tabs.
    _scope_now, _ = global_filter.current()
    with st.container(key="tabs_zone"):
        _freshness_label(_scope_now)
        (tab_backlog, tab_coverage, tab_overview,
         tab_report, tab_leakage, tab_test_design) = st.tabs(
            ["📋 Backlog", "📐 Coverage",
             "🧭 Overview", "📄 Report", "🐞 Leakage", "✨ AI Test Design"]
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
                if not cold:
                    warmup_cache()          # all cache hits: no UI needed
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
                                    # `expanded=True` on EVERY label update.
                                    # Without it Streamlit 1.59 re-sends the
                                    # block with `expanded` cleared, and the
                                    # frontend reads that as closed: the first
                                    # progress tick collapsed the box, so the
                                    # steps were written but never seen.
                                    # Verified against a real 1.59.2 server.
                                    on_label=lambda lbl: _status.update(
                                        label=lbl, expanded=True),
                                )
                                _status.update(label="✅ Dashboard ready",
                                               state="complete", expanded=False)
                    _warm_slot.empty()                       # gone for good
                    _elapsed = time.time() - _t0
                    st.toast(f"Dashboard loaded in {_elapsed:.0f} sec.",
                             icon="✅")
                    _mark_warm()
            except Exception:  # noqa: BLE001
                logger.exception("Warm-up failed")
                st.warning(
                    "⚠️ Part of the data pre-load failed — sections will load "
                    "lazily and may be slower on first view."
                )
            # Cold start: swap the skeleton for the real strip now that the
            # data is warm — best-effort, the strip hides itself on failure.
            if cold:
                try:
                    with kpi_slot.container():
                        kpi_strip.render()
                except Exception:  # noqa: BLE001
                    logger.exception("KPI strip failed to fill in after warm-up")

            # `*_anim` containers opt each tab into the scroll-reveal animation
            # (styles.py) — Coverage wraps itself internally.
            _render_isolated(backlog_tab.render, "Backlog", "backlog_anim")
    except Exception as exc:  # noqa: BLE001 — global safety net, never crash the app
        logger.exception("Unexpected failure in the Backlog tab")
        st.error(f"Unexpected error: {exc}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())

    # Each remaining tab renders in isolation (see `_render_tab`).
    _render_tab(tab_coverage, coverage_tab.render, "Coverage")
    _render_tab(tab_overview, overview_tab.render, "Overview", "overview_anim")
    _render_tab(tab_report,   report_tab.render,   "Report",   "report_anim")
    # Jira only (one read of the last year's incidents, cached 30 min and
    # shared by every session); matched to TestRail through cases already
    # downloaded, so it adds no TestRail request.
    _render_tab(tab_leakage, leakage_tab.render, "Leakage")
    # Beta.  Costs nothing on a normal run: it is one fragment that draws a
    # form, and calls Jira, Confluence or Gemini only when "Generate" is pressed.
    _render_tab(tab_test_design, test_design_tab.render, "AI Test Design")

    # Dexter's snapshot builds OFF the critical path: everything above has
    # already rendered; this line only costs time when its cache is cold
    # (~30s on Cloud, once per TTL) and Dexter's first reply stays instant.
    try:
        from src.ui.chat_assistant import _build_coverage_brief
        _build_coverage_brief()
    except Exception:  # noqa: BLE001 — Dexter rebuilds it on first question
        logger.exception("Pre-building Dexter's coverage snapshot failed")

    # Same idea for the Backlog tiles: building one BU's evidence frame costs
    # ~250ms, and it is the only thing left that a BU switch waits for.  Doing
    # it here — after the page is on screen, from frames that are already
    # cached — trades a few seconds of invisible work for instant tiles on
    # every BU.  NO TestRail calls: it reads the cached expansion.
    #
    # This called `_tile_exports`, which was deleted on 2026-07-30.  The import
    # failed on every run from then on and a bare `pass` swallowed it, so the
    # pre-build silently did nothing for two months.  Hence the log line.
    try:
        from src.ui.backlog_tab import _scoped_bus, _tile_evidence
        for _bu, _scope in _scoped_bus():
            _tile_evidence(_bu, _scope)
    except Exception:  # noqa: BLE001 — each tile builds its own on demand
        logger.exception("Pre-building the Backlog tile evidence failed")


if __name__ == "__main__":
    main()
