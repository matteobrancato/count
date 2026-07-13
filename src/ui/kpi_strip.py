"""Group KPI strip — the 5-second executive summary under the header.

One compact row of chips with RAG dots, always visible above the tabs:

    🟡 Regression coverage 66.2% · 🔴 Backlog 1,369 (12.1%)
    · 🏆 Best: Drogas 95.2% · 🔴 Focus: ICI Paris XL 35.3%

"Coverage" here means AUTOMATED / REGRESSION-BASELINE (the big_regr rows —
exactly the Backlog tab's numbers and Cov. % basis), NOT automated over the
whole case universe: that total-universe figure mixes BUs with huge unlabelled
suites and reads misleadingly low, and management steers on the baseline.

All aggregates come straight from the Backlog pipeline (`_backlog_data`), so
the strip always agrees with the All-BU table.  It deliberately ignores the
scope/BU filter: it is the cross-BU picture (the filter drives the detail tabs).

Rendering is two-phase in app.py: a same-size skeleton holds the slot during
the first load and is replaced when the data is warm — plain markdown, no
Streamlit keys (two keyed containers in one slot raise DuplicateElementKey).
"""
from __future__ import annotations

import logging

import streamlit as st

from .styles import (
    BACKLOG_OK_PCT, COVERAGE_TARGET, backlog_health, coverage_health,
)

logger = logging.getLogger(__name__)


@st.cache_data(ttl=21600, show_spinner=False)
def _kpis() -> dict:
    """Cross-BU regression-baseline aggregates (cached with the data's TTL).

    Raises on failure instead of returning a sentinel — st.cache_data does not
    cache exceptions, so a transient error is retried on the next rerun."""
    from . import backlog_tab as bl

    summary, _, _ = bl._backlog_data()
    if summary.empty:
        return {"per_bu": [], "baseline": None}

    per_bu = [
        {"bu": str(r["BU"]), "pct": float(r["Cov. %"])}
        for _, r in summary.iterrows()
    ]
    per_bu.sort(key=lambda x: -x["pct"])

    baseline = {
        "total":   int(summary["Total"].sum()),
        "auto":    int(summary["Automated"].sum()),
        "backlog": int(summary["Backlog"].sum()),
    }
    return {"per_bu": per_bu, "baseline": baseline}


def _chip(dot: str, label: str, value: str, sub: str = "", tooltip: str = "") -> str:
    sub_html = f"<span class='kpi-sub'>{sub}</span>" if sub else ""
    title    = f" title=\"{tooltip}\"" if tooltip else ""
    return (f"<span class='kpi-chip'{title}>{dot} {label} "
            f"<b>{value}</b>{sub_html}</span>")


def render_skeleton() -> None:
    """Shimmering placeholder with the SAME footprint as the real strip —
    rendered during the first load so the page layout doesn't shift (and the
    strip can't visually merge with the filter bar) when the chips arrive."""
    st.markdown(
        "<div class='kpi-card'><div class='kpi-row'>"
        + "".join("<span class='kpi-skeleton'></span>" for _ in range(4))
        + "</div></div>",
        unsafe_allow_html=True,
    )


def render() -> None:
    """Render the strip; hides itself (no gap) if the aggregates aren't ready."""
    try:
        k = _kpis()
    except Exception:                                                   # noqa: BLE001
        logger.exception("KPI strip aggregates failed")
        return
    base = k["baseline"]
    if not k["per_bu"] or not base or not base["total"]:
        return

    chips: list[str] = []

    pct = base["auto"] / base["total"] * 100
    dot, _c = coverage_health(pct)
    chips.append(_chip(
        dot, "Regression coverage", f"{pct:.1f}%",
        sub=f"{base['auto']:,} / {base['total']:,} rows",
        tooltip=(f"Automated share of the big_regr baseline rows, all BUs — "
                 f"same numbers as the Backlog tab. Target {COVERAGE_TARGET:.0f}%."),
    ))

    kpct = base["backlog"] / base["total"] * 100
    dot, _c = backlog_health(kpct)
    chips.append(_chip(
        dot, "Backlog", f"{base['backlog']:,}",
        sub=f"{kpct:.1f}% of baseline",
        tooltip=(f"Pure backlog rows in the regression baseline "
                 f"(healthy ≤ {BACKLOG_OK_PCT:.0f}%)."),
    ))

    best, worst = k["per_bu"][0], k["per_bu"][-1]
    chips.append(_chip("🏆", "Best", f"{best['bu']} {best['pct']:.1f}%",
                       tooltip="Highest regression-baseline coverage."))
    dot, _c = coverage_health(worst["pct"])
    chips.append(_chip(dot, "Focus", f"{worst['bu']} {worst['pct']:.1f}%",
                       tooltip="Lowest regression-baseline coverage — needs attention."))

    st.markdown(
        f"<div class='kpi-card'><div class='kpi-row'>{''.join(chips)}</div></div>",
        unsafe_allow_html=True,
    )
