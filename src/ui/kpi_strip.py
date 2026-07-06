"""Group KPI strip — the 5-second executive summary under the header.

One compact row of chips with RAG dots, always visible above the tabs:

    🟢 Group coverage 68.2% · 🟢 Baseline 87.4% · 🟡 Backlog 1,204 rows
    · Best: Drogas 93.6% · Focus: ICI Paris XL 35.3%

Data comes from the SAME cached building blocks the tabs and Dexter use
(`get_bu_coverage` per BU + the Backlog pipeline), aggregated once per data
refresh — so the strip always agrees with the rest of the dashboard and adds
no extra TestRail calls.  It deliberately ignores the scope/BU filter: it is
the cross-BU picture (the filter drives the detail tabs below).

Rendering is two-phase in app.py: an empty slot is reserved under the header
and FILLED after the warm-up completes, so on a cold start the page skeleton
appears instantly and the strip pops in with the data.
"""
from __future__ import annotations

import logging

import streamlit as st

from ..bu_rules import ALL_RULES
from .styles import (
    BACKLOG_OK_PCT, COVERAGE_TARGET, backlog_health, coverage_health,
)

logger = logging.getLogger(__name__)


@st.cache_data(ttl=3600, show_spinner=False)
def _kpis() -> dict:
    """Cross-BU aggregates for the strip (cached with the data's TTL).

    Raises on failure instead of returning a sentinel — st.cache_data does not
    cache exceptions, so a transient error is retried on the next rerun."""
    from . import backlog_tab as bl
    from . import chat_assistant as ca

    per_bu: list[dict] = []
    grand_total = grand_auto = 0
    for bu in sorted({r.bu for r in ALL_RULES}):
        d = ca.get_bu_coverage(bu)
        if not isinstance(d, dict) or "error" in d:
            continue
        per_bu.append({"bu": d["business_unit"], "pct": float(d["coverage_pct"])})
        grand_total += int(d.get("total_cases") or 0)
        grand_auto  += int(d.get("automated_unique") or 0)
    per_bu.sort(key=lambda x: -x["pct"])

    baseline = None
    summary, _, _ = bl._backlog_data()
    if not summary.empty:
        baseline = {
            "total":   int(summary["Total"].sum()),
            "auto":    int(summary["Automated"].sum()),
            "backlog": int(summary["Backlog"].sum()),
        }

    return {
        "per_bu":      per_bu,
        "grand_total": grand_total,
        "grand_auto":  grand_auto,
        "baseline":    baseline,
    }


def _chip(dot: str, label: str, value: str, sub: str = "", tooltip: str = "") -> str:
    sub_html = f"<span class='kpi-sub'>{sub}</span>" if sub else ""
    title    = f" title=\"{tooltip}\"" if tooltip else ""
    return (f"<span class='kpi-chip'{title}>{dot} {label} "
            f"<b>{value}</b>{sub_html}</span>")


def render_skeleton() -> None:
    """Shimmering placeholder with the SAME footprint as the real strip —
    rendered during the first load so the page layout doesn't shift (and the
    strip can't visually merge with the filter bar) when the chips arrive."""
    with st.container(key="kpi_strip"):
        st.markdown(
            "<div class='kpi-row'>"
            + "".join("<span class='kpi-skeleton'></span>" for _ in range(5))
            + "</div>",
            unsafe_allow_html=True,
        )


def render() -> None:
    """Render the strip; hides itself (no gap) if the aggregates aren't ready."""
    try:
        k = _kpis()
    except Exception:                                                   # noqa: BLE001
        logger.exception("KPI strip aggregates failed")
        return
    if not k["per_bu"]:
        return

    chips: list[str] = []

    if k["grand_total"]:
        pct = k["grand_auto"] / k["grand_total"] * 100
        dot, _c = coverage_health(pct)
        chips.append(_chip(
            dot, "Group coverage", f"{pct:.1f}%",
            sub=f"{k['grand_auto']:,} / {k['grand_total']:,} cases",
            tooltip=(f"Unique automated cases across all BUs. "
                     f"Target {COVERAGE_TARGET:.0f}%."),
        ))

    base = k["baseline"]
    if base and base["total"]:
        bpct = base["auto"] / base["total"] * 100
        dot, _c = coverage_health(bpct)
        chips.append(_chip(
            dot, "Regression baseline", f"{bpct:.1f}%",
            sub=f"{base['auto']:,} / {base['total']:,} rows",
            tooltip="Automated share of the big_regr baseline rows "
                    "(same numbers as the Backlog tab).",
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
                       tooltip="Highest overall coverage."))
    dot, _c = coverage_health(worst["pct"])
    chips.append(_chip(dot, "Focus", f"{worst['bu']} {worst['pct']:.1f}%",
                       tooltip="Lowest overall coverage — needs attention."))

    with st.container(key="kpi_strip"):
        st.markdown(f"<div class='kpi-row'>{''.join(chips)}</div>",
                    unsafe_allow_html=True)
