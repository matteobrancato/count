from __future__ import annotations

import logging
import time
import traceback
from contextlib import contextmanager

import streamlit as st

import deploy_guard

# Before anything from src/: after a deploy, drop the modules imported from the
# previous code, or the new app.py runs against them (see deploy_guard).
deploy_guard.reload_after_deploy()

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
    styles,
)
from src.ui.styles import COLORS

logger = logging.getLogger(__name__)


st.set_page_config(
    page_title="Automation Dashboard",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# -------------------------------------------------------------------- timings
# Where one run's time goes, section by section.  Module-level on purpose:
# Streamlit re-executes this script from scratch for every run, so the list
# starts empty each time and belongs to exactly one run — the same property
# that made a lock here useless makes this list correct.  Shown at the bottom
# of the page with ?debug=1; logged whenever a run is slow.
_TIMINGS: list[tuple[str, float]] = []
_SLOW_RUN_SECONDS = 5.0


@contextmanager
def _timed(label: str):
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _TIMINGS.append((label, time.perf_counter() - t0))


def _report_timings(total: float) -> None:
    ranked = sorted(_TIMINGS, key=lambda item: -item[1])
    if total >= _SLOW_RUN_SECONDS:
        logger.warning("Slow run: %.1fs — %s", total,
                       ", ".join(f"{label} {secs:.2f}s" for label, secs in ranked[:6]))
    if st.query_params.get("debug") == "1":
        rows = "".join(f"| {label} | {secs:.3f} |\n" for label, secs in ranked)
        st.markdown(f"**Run timings** — {total:.2f}s total\n\n"
                    f"| section | seconds |\n|---|---:|\n{rows}")


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


_LOGO = (
    f"<div style='width:38px;height:38px;border-radius:11px;"
    f"display:flex;align-items:center;justify-content:center;font-size:20px;"
    f"background:linear-gradient(135deg,{COLORS['brand']} 0%,{COLORS['brand_strong']} 100%);"
    f"box-shadow:0 3px 10px rgba(46,91,255,0.28)'>🧪</div>"
)
# A <span>, not an <h1>: Streamlit gives a markdown block a -1rem bottom margin
# that a <p> is meant to pay back, and a bare block element does not — the
# title then collapsed onto the KPI line under it.  Inline text is wrapped in
# a <p>, which restores the balance (same fix as the utility bar's label).
_TITLE = ("<span style='font-size:24px;font-weight:700;line-height:1.1;"
          "white-space:nowrap;letter-spacing:-0.01em'>Automation Coverage</span>")


def _header():
    """Logo and title, with the cross-BU KPI chips as the line under the title
    (where a decorative subtitle used to be).  Returns the st.empty slot the
    chips live in, so a cold start can swap the skeleton for the real chips."""
    with st.container(key="brand", horizontal=True, width="content",
                      vertical_alignment="center", gap="small"):
        st.markdown(_LOGO, unsafe_allow_html=True, width="content")
        with st.container(key="brand_text", width="content", gap=None):
            st.markdown(_TITLE, unsafe_allow_html=True, width="content")
            return st.empty()


@st.dialog("🧭 Overview", width="large")
def _overview_dialog() -> None:
    """Cross-BU automated totals — a window, not a tab.  Its body runs only
    while it is open: as a tab it re-ran on every visit, and it answers a
    question people ask now and then, not the one they open the page for."""
    if not _is_warm():
        st.caption("Available once the data has finished loading.")
        return
    overview_tab.render()


def _freshness_label(scope: str = "website") -> None:
    """Utility bar pinned (`.st-key-freshness`) to the tab-bar's top-right:

        ℹ️ How numbers are calculated · 🧹 Data quality · Updated 3m ago ↻ · 🧭 Overview

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
        # Styled as the two text links above (`.st-key-overview_open`).  A
        # button + dialog rather than a popover: a popover's body runs on every
        # rerun, open or not, and the Overview is not free to draw.
        if st.button("🧭 Overview", key="overview_open"):
            _overview_dialog()


# -------------------------------------------------------------------- credentials gate
def _creds_ok() -> bool:
    try:
        tr.TestRailCredentials.from_secrets()
        return True
    except tr.TestRailError as exc:
        _header()          # the brand without chips: there is nothing to count
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
        with _timed(label):
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


# -------------------------------------------------------------------- sections
# (tab label, renderer, animation container key).  One table instead of six
# hand-written calls, so adding or reordering a tab is a one-line change.
_SECTIONS = [
    ("📋 Backlog",        backlog_tab.render,        "backlog_anim"),
    # The Business Unit picked in the top bar, one BU at a time.
    ("🔎 BU Detail",      backlog_tab.render_detail, "detail_anim"),
    ("📐 Coverage",       coverage_tab.render,       ""),          # wraps itself
    # Jira only, matched to TestRail through cases already downloaded — the
    # tab never calls TestRail.  (The Overview is a window in the utility bar.)
    ("🐞 Leakage",        leakage_tab.render,        ""),
]


def _warm_up(cold: bool, kpi_slot) -> None:
    """Make sure the day's data is loaded, showing the loader if it is not.

    Warm: nothing at all.  Each section asks for the data it uses and gets a
    cache hit; calling the warm-up anyway cost 0.13-0.17 s on EVERY click,
    because st.cache_data hands back a copy (an unpickle) of the big frames on
    every hit — measured live, 45% of a Backlog rerun.
    Cold: a step-by-step status that lives in an
    st.empty slot and is REMOVED when done (a CSS-hidden box stayed in the DOM
    and replayed its animation on every tab switch), then a transient toast.
    A failure never blanks the tab — worst case each section fetches lazily
    and surfaces its own error.
    """
    if not cold:
        return
    from src.rules_engine import warmup_cache
    try:
        slot = st.empty()
        t0 = time.time()
        with _timed("Warm-up (loading)"), slot.container(), \
                st.container(key="warmup_status"), \
                st.status("⚡ Loading dashboard data…", expanded=True) as status:
            warmup_cache(
                on_step=status.write,
                # `expanded=True` on EVERY label update.  Without it Streamlit
                # 1.59 re-sends the block with `expanded` cleared, and the
                # frontend reads that as closed: the first progress tick
                # collapsed the box, so the steps were written but never seen.
                # Verified against a real 1.59.2 server.
                on_label=lambda lbl: status.update(label=lbl, expanded=True),
            )
            status.update(label="✅ Dashboard ready", state="complete",
                          expanded=False)
        slot.empty()
        elapsed = time.time() - t0
        # Only when there was a load to speak of: a new visitor on data that is
        # already warm used to be told "Dashboard loaded in 0 sec."
        if elapsed >= 2:
            st.toast(f"Dashboard loaded in {elapsed:.0f} sec.", icon="✅")
        _mark_warm()
    except Exception:  # noqa: BLE001
        logger.exception("Warm-up failed")
        st.warning("⚠️ Part of the data pre-load failed — sections will load "
                   "lazily and may be slower on first view.")
    # Cold start: swap the KPI skeleton for the real strip now that the data
    # is warm — best-effort, the strip hides itself on failure.
    if cold:
        try:
            with kpi_slot.container():
                kpi_strip.render()
        except Exception:  # noqa: BLE001
            logger.exception("KPI strip failed to fill in after warm-up")


# -------------------------------------------------------------------- main
def main() -> None:
    _t_main = time.perf_counter()
    with _timed("Styles"):
        styles.inject()   # global design system — purely cosmetic, must run first.
    if not _creds_ok():
        st.stop()

    # The first run of a new business day clears yesterday's numbers; every
    # other run of the day is a cache hit.  Decided before anything is drawn,
    # so the freshness bar, the KPI strip and the loader all agree on it.
    try:
        with _timed("Daily rollover check"):
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
        with _timed("Account pool"):
            tr.ensure_pool()
    except Exception:  # noqa: BLE001 — the fetches surface credential errors
        logger.exception("Could not build the TestRail account pool")

    # Render the floating chat FIRST — Streamlit renders incrementally, so
    # placing it here makes the FAB appear immediately, before the (slow) data
    # fetches in the tab renders below.  `position: fixed` in the CSS handles the
    # visual placement, so DOM order doesn't matter.
    try:
        with _timed("Dexter button"):
            chat_assistant.render_floating_button()
    except Exception:  # noqa: BLE001 — never let the chat break the app
        logger.exception("Dexter's button failed to render")

    # NOTE on load UX: we create the tab bar FIRST (instant skeleton), then warm
    # the whole cache inside the active tab below (not in a blocking pre-fetch
    # before st.tabs(), which used to leave the tab area blank/white).  So the
    # page chrome is visible immediately and the loader sits on the data area.

    # ONE top bar: the brand (title, with the two cross-BU KPI chips as the
    # line under it) on the left, the scope / BU controls every tab reads on
    # the right.  It replaced three stacked bands — title, KPI card, filter
    # card — that pushed the data a third of a screen down.  Horizontal
    # containers wrap, so a narrow window gets two tidy rows, not an overflow.
    #
    # The KPI chips sit in an st.empty slot: warm runs fill it immediately; on
    # a cold start a same-size shimmering skeleton holds the space and is
    # REPLACED after the warm-up, so the layout never shifts.
    with st.container(key="topbar", horizontal=True, vertical_alignment="center",
                      horizontal_alignment="distribute", gap="medium"):
        with _timed("Header"):
            kpi_slot = _header()
        try:
            with kpi_slot.container(), _timed("KPI strip"):
                if cold:
                    kpi_strip.render_skeleton()
                else:
                    kpi_strip.render()
        except Exception:  # noqa: BLE001
            logger.exception("KPI strip failed to render")
        with st.container(key="topbar_controls", horizontal=True, width="content",
                          vertical_alignment="center", gap="small"), \
                _timed("Global filter"):
            global_filter.render()

    # Wrap the tab bar in a relative-positioned zone so the freshness label can
    # be pinned to its top-right (= the tab row), reliably level with the tabs.
    _scope_now, _ = global_filter.current()
    with st.container(key="tabs_zone"), _timed("Freshness bar + tab bar"):
        _freshness_label(_scope_now)
        # LAZY tabs: only the selected one executes.  Streamlit otherwise runs
        # every tab body on every interaction — measured live on 2026-09-26,
        # 2.15 s per click with all caches warm, 87% of it in tabs nobody was
        # looking at.  Switching tab now costs a rerun of that one tab instead
        # of being free; every other click costs one tab instead of all of them.
        tabs = st.tabs([label for label, _fn, _anim in _SECTIONS],
                       key="section", on_change="rerun")

    open_index = next((i for i, t in enumerate(tabs) if t.open), 0)
    label, render_fn, anim_key = _SECTIONS[open_index]
    try:
        with tabs[open_index]:
            # The data every tab reads is warmed here, in whichever tab is on
            # screen, so the loader shows where the user is looking and the
            # page is never blank.  Data stays shared: switching tab renders,
            # it does not re-download.
            _warm_up(cold, kpi_slot)
            _render_isolated(render_fn, label, anim_key)
    except Exception as exc:  # noqa: BLE001 — global safety net, never crash the app
        logger.exception("Unexpected failure in the %s tab", label)
        st.error(f"Unexpected error: {exc}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())

    # Pre-builds, only on the run that just loaded the data: on a warm session
    # everything below is already built, and even cache hits cost a little.
    if cold:
        _prebuild()

    _report_timings(time.perf_counter() - _t_main)


def _prebuild() -> None:
    """Start the background pre-build — never waited on (see rules_engine)."""
    try:
        from src.rules_engine import prebuild_in_background
        with _timed("Pre-build started (background)"):
            prebuild_in_background()
    except Exception:  # noqa: BLE001 — each section builds its own on demand
        logger.exception("Could not start the background pre-build")


if __name__ == "__main__":
    main()
