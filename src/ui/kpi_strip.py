"""Group KPIs — the cross-BU figures, as four cards under the top bar.

    Coverage 78.4%   Backlog 1,058 · 4.2%   Best coverage …   Needs attention …

Each card says what its figure is a share of (rows of the baseline), where the
target or limit is (a tick on a meter) and the colour it earns against it.  They
were chips under the title on one line that scrolled sideways; cards fit the page
at any width (a grid that wraps) and need no tooltip to say what they mean.

"Coverage" here means AUTOMATED / REGRESSION-BASELINE (the big_regr rows —
exactly the Backlog tab's numbers and Cov. % basis), NOT automated over the
whole case universe: that total-universe figure mixes BUs with huge unlabelled
suites and reads misleadingly low, and management steers on the baseline.

All four are kept (Matteo, 2026-09-28: keep Best and Focus rather than drop them).

All aggregates come straight from the Backlog pipeline (`_backlog_data`), so
the cards always agree with the All-BU table.  They deliberately ignore the
scope/BU filter: they are the cross-BU picture.

Rendering is two-phase in app.py: a same-size skeleton holds the slot during
the first load and is replaced when the data is warm — plain markdown, no
Streamlit keys (two keyed containers in one slot raise DuplicateElementKey).
"""
from __future__ import annotations

import html
import logging

import streamlit as st

from ..freshness import DAY_TTL, computed_cache
from .styles import (
    BACKLOG_OK_PCT,
    COVERAGE_TARGET,
    backlog_health,
    coverage_health,
)

logger = logging.getLogger(__name__)


@computed_cache(ttl=DAY_TTL, show_spinner=False)
def _kpis() -> dict:
    """Cross-BU regression-baseline aggregates (cached with the data's TTL).

    Raises on failure instead of returning a sentinel — st.cache_data does not
    cache exceptions, so a transient error is retried on the next rerun."""
    from . import backlog_tab as bl

    summary, _, _ = bl._backlog_data()
    if summary.empty:
        return {"per_bu": [], "baseline": None}

    per_bu = [
        {"bu": str(r["BU"]), "pct": float(r["Coverage %"])}
        for _, r in summary.iterrows()
    ]
    per_bu.sort(key=lambda x: -x["pct"])

    baseline = {
        "total":   int(summary["Total"].sum()),
        "auto":    int(summary["Automated"].sum()),
        "backlog": int(summary["Backlog"].sum()),
    }
    return {"per_bu": per_bu, "baseline": baseline}


def _meter(pct: float, color: str, tick: float) -> str:
    """A 0-100% bar filled to *pct*, with a tick where the target (or the limit)
    is.  Clamped: a share cannot leave its track."""
    return (f"<div class='kpi-meter'><i style='width:{max(0.0, min(pct, 100.0)):.1f}%;"
            f"background:{color}'></i><b style='left:{tick:.1f}%'></b></div>")


def _card(label: str, value: str, color: str, meter: str, sub: str, *,
          scope: str = "", small: str = "") -> str:
    scope_html = f"<span class='kpi-scope'>{scope}</span>" if scope else ""
    small_html = f"<small style='color:{color}'>{small}</small>" if small else ""
    return (f"<div class='kpi-card'>"
            f"<div class='kpi-label'><span class='kpi-dot' style='background:{color}'>"
            f"</span>{label}{scope_html}</div>"
            f"<div class='kpi-value'>{value}{small_html}</div>"
            f"{meter}<div class='kpi-sub'>{sub}</div></div>")


def render_skeleton() -> None:
    """Shimmering placeholder with the SAME footprint as the real cards, so the
    page does not shift when they arrive after the first load."""
    st.markdown(
        "<div class='kpi-grid'>"
        + "".join("<div class='kpi-skeleton-card'></div>" for _ in range(4))
        + "</div>",
        unsafe_allow_html=True,
    )


def render() -> None:
    """Render the cards; hides itself (no gap) if the aggregates aren't ready."""
    try:
        k = _kpis()
    except Exception:                                                   # noqa: BLE001
        logger.exception("KPI strip aggregates failed")
        return
    base = k["baseline"]
    if not k["per_bu"] or not base or not base["total"]:
        return

    pct = base["auto"] / base["total"] * 100
    kpct = base["backlog"] / base["total"] * 100
    best, worst = k["per_bu"][0], k["per_bu"][-1]
    _, cov_color = coverage_health(pct)
    _, bl_color = backlog_health(kpct)
    _, worst_color = coverage_health(worst["pct"])
    _, best_color = coverage_health(best["pct"])
    cards = [
        _card("Coverage", f"{pct:.1f}%", cov_color,
              _meter(pct, cov_color, COVERAGE_TARGET),
              f"<strong>{base['auto']:,}</strong> of {base['total']:,} baseline rows "
              f"· target {COVERAGE_TARGET:.0f}%", scope="all BUs"),
        _card("Backlog", f"{base['backlog']:,}", bl_color,
              _meter(kpct, bl_color, BACKLOG_OK_PCT),
              f"rows automated nowhere · healthy ≤ {BACKLOG_OK_PCT:.0f}%",
              scope="all BUs", small=f"{kpct:.1f}%"),
        _card("Best coverage", f"{best['pct']:.1f}%", best_color,
              _meter(best["pct"], best_color, COVERAGE_TARGET),
              f"<strong>{html.escape(best['bu'])}</strong>"),
        _card("Needs attention", f"{worst['pct']:.1f}%", worst_color,
              _meter(worst["pct"], worst_color, COVERAGE_TARGET),
              f"<strong>{html.escape(worst['bu'])}</strong> · lowest coverage"),
    ]
    st.markdown(f"<div class='kpi-grid'>{''.join(cards)}</div>", unsafe_allow_html=True)
