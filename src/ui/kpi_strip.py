"""Group KPIs — the two cross-BU figures, as chips in the top bar.

    🟡 Coverage 79.4% all BUs   🟢 Backlog 837 3.3%

"Coverage" here means AUTOMATED / REGRESSION-BASELINE (the big_regr rows —
exactly the Backlog tab's numbers and Cov. % basis), NOT automated over the
whole case universe: that total-universe figure mixes BUs with huge unlabelled
suites and reads misleadingly low, and management steers on the baseline.

Only the two aggregates the page shows nowhere else.  "Best" and "Focus" (the
top and bottom BU) were dropped on 2026-09-28: the All-BU table right below
shows every BU's coverage in RAG colour, so they repeated it in less detail.

All aggregates come straight from the Backlog pipeline (`_backlog_data`), so
the chips always agree with the All-BU table.  They deliberately ignore the
scope/BU filter: they are the cross-BU picture.

Rendering is two-phase in app.py: a same-size skeleton holds the slot during
the first load and is replaced when the data is warm — plain markdown, no
Streamlit keys (two keyed containers in one slot raise DuplicateElementKey).
"""
from __future__ import annotations

import logging

import streamlit as st

from ..freshness import DAY_TTL
from .styles import (
    BACKLOG_OK_PCT,
    COVERAGE_TARGET,
    backlog_health,
    coverage_health,
)

logger = logging.getLogger(__name__)


@st.cache_data(ttl=DAY_TTL, show_spinner=False)
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


def _chip(dot: str, label: str, value: str, sub: str = "", tooltip: str = "") -> str:
    sub_html = f"<span class='kpi-sub'>{sub}</span>" if sub else ""
    title    = f" title=\"{tooltip}\"" if tooltip else ""
    return (f"<span class='kpi-chip'{title}>{dot} {label} "
            f"<b>{value}</b>{sub_html}</span>")


def render_skeleton() -> None:
    """Shimmering placeholder with the SAME footprint as the real chips, so
    the top bar does not shift when they arrive after the first load."""
    st.markdown(
        "<div class='kpi-row'>"
        + "".join("<span class='kpi-skeleton'></span>" for _ in range(2))
        + "</div>",
        unsafe_allow_html=True, width="content",
    )


def render() -> None:
    """Render the chips; hides itself (no gap) if the aggregates aren't ready."""
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
    chips = [
        _chip(coverage_health(pct)[0], "Coverage", f"{pct:.1f}%", sub="all BUs",
              tooltip=(f"Automated share of the big_regr baseline rows, all BUs: "
                       f"{base['auto']:,} of {base['total']:,} rows — same numbers "
                       f"as the Backlog tab. Target {COVERAGE_TARGET:.0f}%.")),
        _chip(backlog_health(kpct)[0], "Backlog", f"{base['backlog']:,}",
              sub=f"{kpct:.1f}%",
              tooltip=(f"Baseline rows whose case is automated NOWHERE — a script "
                       f"to write from scratch — {kpct:.1f}% of the baseline, all "
                       f"BUs. Healthy ≤ {BACKLOG_OK_PCT:.0f}%.")),
    ]
    st.markdown(f"<div class='kpi-row'>{''.join(chips)}</div>",
                unsafe_allow_html=True, width="content")
