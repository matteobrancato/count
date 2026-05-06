"""Report tab — clean, screenshot-ready automation coverage report.

Reproduces the 'Automation – Frameworks and Status' PowerPoint slide:
  • Header  : framework cards (Java, TestIM) + key metric badges
  • Chart   : horizontal bar chart — automated cases per BU × country × device
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from .. import metrics
from ..bu_rules import ALL_RULES
from ..rules_engine import evaluate_rules

# ── palette ───────────────────────────────────────────────────────────────────
_BLUE   = "#4472C4"   # Mobile
_ORANGE = "#ED7D31"   # Desktop
_RED    = "#C00000"   # metric numbers

_BU_ORDER = [
    "The Perfume Shop", "Savers", "Superdrug",
    "Kruidvat", "Trekplaister", "Watsons", "Drogas",
    "Marionnaud", "ICI Paris XL",
    "Next Gen",
]


# ── data ──────────────────────────────────────────────────────────────────────
def _load() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (website_auto, all_auto) both deduplicated."""
    frames_all: list[pd.DataFrame] = []
    frames_web: list[pd.DataFrame] = []

    for scope in ("website", "next_gen"):
        rules = [r for r in ALL_RULES if r.scope == scope]
        if not rules:
            continue
        result = evaluate_rules(tuple(r.name for r in rules))
        if result.automated.empty:
            continue
        frames_all.append(result.automated)
        if scope == "website":
            frames_web.append(result.automated)

    def _dedup(frames: list[pd.DataFrame]) -> pd.DataFrame:
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames, ignore_index=True)
        return df.drop_duplicates(subset=["bu", "country_label", "device", "case_id"])

    return _dedup(frames_web), _dedup(frames_all)


# ── chart ─────────────────────────────────────────────────────────────────────
def _build_chart(auto: pd.DataFrame) -> alt.Chart:
    """Horizontal bar chart grouped by BU, coloured by device."""
    grp = (
        auto.groupby(["bu", "country_label", "device"])["case_id"]
        .nunique()
        .reset_index(name="count")
    )

    # Ordered BU list
    present = set(grp["bu"].unique())
    bus = [b for b in _BU_ORDER if b in present]
    bus += sorted(b for b in present if b not in bus)

    # Build a flat DataFrame with a sort key for the y-axis.
    # Row label = "device country" e.g. "mobile LT".
    # bu_rank controls BU grouping order; ctry_rank controls country order within BU.
    rows: list[dict] = []
    bu_rank = {b: i for i, b in enumerate(bus)}

    for bu in bus:
        bu_data   = grp[grp["bu"] == bu]
        countries = sorted(bu_data["country_label"].unique())
        for ci, ctry in enumerate(countries):
            for di, device in enumerate(["Mobile", "Desktop"]):  # Mobile on top within pair
                row = bu_data[
                    (bu_data["country_label"] == ctry) &
                    (bu_data["device"] == device)
                ]
                count = int(row["count"].iloc[0]) if not row.empty else 0
                rows.append({
                    "bu":       bu,
                    "label":    f"{device.lower()} {ctry}",
                    "device":   device,
                    "country":  ctry,
                    "count":    count,
                    # sort_key: lower = higher on chart (Altair sorts ascending by default,
                    # so we reverse: higher sort_key = lower on chart)
                    "sort_key": bu_rank[bu] * 1000 + ci * 10 + di,
                })

    df = pd.DataFrame(rows)

    # Altair y-axis order: provide explicit sorted list of labels
    # sort ascending by sort_key means lower sort_key = top of chart
    label_order = (
        df.sort_values("sort_key")["label"].tolist()
    )

    color_scale = alt.Scale(
        domain=["Mobile", "Desktop"],
        range=[_BLUE, _ORANGE],
    )

    bars = (
        alt.Chart(df)
        .mark_bar(height=14)
        .encode(
            x=alt.X("count:Q",
                    axis=alt.Axis(title=None, grid=True, gridColor="#f0f0f0",
                                  tickCount=6, labelFontSize=10)),
            y=alt.Y("label:N",
                    sort=label_order,
                    axis=alt.Axis(title=None, labelFontSize=10.5,
                                  labelFont="Arial", labelLimit=180,
                                  ticks=False, domain=False)),
            color=alt.Color("device:N",
                            scale=color_scale,
                            legend=alt.Legend(
                                title=None, orient="top-right",
                                labelFontSize=12, symbolSize=120,
                                direction="horizontal",
                            )),
            tooltip=[
                alt.Tooltip("bu:N",      title="BU"),
                alt.Tooltip("country:N", title="Country"),
                alt.Tooltip("device:N",  title="Device"),
                alt.Tooltip("count:Q",   title="Automated", format=","),
            ],
        )
    )

    text = (
        alt.Chart(df)
        .mark_text(align="left", dx=4, fontSize=9.5, color="#444444")
        .encode(
            x=alt.X("count:Q"),
            y=alt.Y("label:N", sort=label_order),
            text=alt.Text("count:Q", format=","),
        )
    )

    # BU label annotations — one row per BU as a rule/text separator
    bu_df = (
        df.groupby("bu")["sort_key"]
        .min()
        .reset_index()
        .rename(columns={"sort_key": "first_label_key"})
    )
    # The BU header should appear just above the first row of that BU
    # We do this by drawing text at the first label of each BU group.
    bu_label_map = (
        df.sort_values("sort_key")
        .groupby("bu")["label"]
        .first()
        .reset_index()
    )

    chart = (
        alt.layer(bars, text)
        .properties(
            width="container",
            height=max(400, len(df) * 20),
        )
        .configure_view(strokeWidth=0)
        .configure_axis(labelFont="Arial", titleFont="Arial")
        .configure_legend(labelFont="Arial")
    )

    return chart


# ── UI helpers ────────────────────────────────────────────────────────────────
def _fw_card(col, icon: str, name: str, subtitle: str, bg: str) -> None:
    col.markdown(
        f"""
        <div style="background:{bg};border:1px solid #ddd;border-radius:10px;
                    padding:14px 16px;display:flex;align-items:center;gap:12px;
                    min-height:68px">
            <span style="font-size:26px;line-height:1">{icon}</span>
            <div>
                <div style="font-weight:700;font-size:13.5px;color:#1a1f36">{name}</div>
                <div style="font-size:11px;color:#5e6677;margin-top:2px">{subtitle}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _metric_badge(col, value: str, label: str, sub: str = "") -> None:
    sub_html = (
        f'<div style="font-size:9.5px;color:#888;margin-top:1px">{sub}</div>'
        if sub else ""
    )
    col.markdown(
        f"""
        <div style="background:white;border:1px solid #ddd;border-radius:10px;
                    padding:12px 14px;text-align:center;min-height:68px;
                    display:flex;flex-direction:column;justify-content:center">
            <div style="font-size:23px;font-weight:800;color:{_RED};line-height:1.1">{value}</div>
            <div style="font-size:10px;font-weight:600;color:#1a1f36;margin-top:2px">{label}</div>
            {sub_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── render ────────────────────────────────────────────────────────────────────
def render() -> None:
    st.markdown(
        "<style>.block-container{padding-top:1rem;padding-bottom:0.5rem}</style>",
        unsafe_allow_html=True,
    )

    st.markdown(
        "<h2 style='text-align:center;font-family:Arial;font-weight:800;"
        "color:#1a1f36;margin-bottom:20px;letter-spacing:-0.5px'>"
        "Automation &nbsp;·&nbsp; Frameworks and Status</h2>",
        unsafe_allow_html=True,
    )

    with st.spinner("Loading…"):
        web_auto, all_auto = _load()

    if all_auto.empty:
        st.warning("No automated data available. Click 🔄 Refresh.")
        return

    smoke = metrics.select_smoke(web_auto)
    s_tot = metrics.totals(smoke)
    a_tot = metrics.totals(all_auto)

    # ── Header: frameworks + metric badges ────────────────────────────────────
    c_fw1, c_fw2, _sp, c_m1, c_m2 = st.columns([2.3, 2.3, 0.15, 1.3, 1.3])

    _fw_card(c_fw1, "☕",
             "Java  /  Selenium  /  Cucumber",
             "Legacy framework used by aLab",
             "#FFFBEC")
    _fw_card(c_fw2, "🤖",
             "TestIM",
             "AI powered test automation platform",
             "#EBF2FF")
    _metric_badge(c_m1, f"+{s_tot['total']:,}", "Test Cases", "Smoke Suite")
    _metric_badge(c_m2, f"+{a_tot['total']:,}", "Test Cases", "Total Count")

    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

    # ── BU grouping headers + chart ───────────────────────────────────────────
    st.markdown(
        "<div style='font-family:Arial;font-weight:700;font-size:15px;"
        "color:#1a1f36;border-left:4px solid #ED7D31;"
        "padding-left:10px;margin-bottom:8px'>"
        "📊 Automated Tests by Business Unit *</div>",
        unsafe_allow_html=True,
    )

    # BU group labels rendered as HTML above the chart
    # (Altair doesn't support mixed bold/normal y-axis without a facet)
    grp = (
        all_auto.groupby(["bu", "country_label", "device"])["case_id"]
        .nunique()
        .reset_index(name="count")
    )
    present = set(grp["bu"].unique())
    bus_present = [b for b in _BU_ORDER if b in present]
    bus_present += sorted(b for b in present if b not in bus_present)

    chart = _build_chart(all_auto)
    st.altair_chart(chart, use_container_width=True)

    st.markdown(
        "<div style='font-size:10.5px;color:#888;margin-top:2px'>"
        "* One row per test case × country × device — "
        "deduplicated within each (BU, country, device)."
        "</div>",
        unsafe_allow_html=True,
    )
