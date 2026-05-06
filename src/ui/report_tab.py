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
    "Marionnaud", "ICI Paris XL", "Next Gen",
]


# ── data loading (shares evaluate_rules cache with other tabs) ────────────────
def _load() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (website_auto, all_auto) deduplicated on (bu, country, device, case_id)."""
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
        return (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["bu", "country_label", "device", "case_id"])
        )

    return _dedup(frames_web), _dedup(frames_all)


# ── chart ─────────────────────────────────────────────────────────────────────
def _prepare_chart_data(auto: pd.DataFrame, bus: list[str]) -> pd.DataFrame:
    """Aggregate and annotate data for the Altair chart."""
    grp = (
        auto.groupby(["bu", "country_label", "device"])["case_id"]
        .nunique()
        .reset_index(name="count")
    )
    # Assign sort keys vectorised
    grp["ctry_rank"] = (
        grp.groupby("bu")["country_label"]
        .transform(lambda s: s.map({c: i for i, c in enumerate(sorted(s.unique()))}))
    )
    grp["dev_rank"]  = grp["device"].map({"Mobile": 0, "Desktop": 1})
    grp["sort_key"]  = grp["ctry_rank"] * 10 + grp["dev_rank"]
    grp["label"]     = grp["device"].str.lower() + " " + grp["country_label"]
    grp["bu_rank"]   = grp["bu"].map({b: i for i, b in enumerate(bus)})

    return grp.rename(columns={"country_label": "country"})


def _build_chart(auto: pd.DataFrame) -> tuple[alt.Chart, list[str]]:
    """Return (faceted Altair chart, ordered BU list)."""
    present = set(auto["bu"].unique())
    bus = [b for b in _BU_ORDER if b in present]
    bus += sorted(b for b in present if b not in bus)

    df = _prepare_chart_data(auto, bus)

    # Sort y-axis per-facet by sort_key (field-based, no global list).
    # A global label list would contain duplicates (same country code in multiple BUs)
    # and cause Altair to bleed data across facet panels.
    y_sort = alt.EncodingSortField(field="sort_key", order="ascending")

    color_scale = alt.Scale(domain=["Mobile", "Desktop"], range=[_BLUE, _ORANGE])

    y_axis = alt.Axis(title=None, labelFontSize=10.5, labelFont="Arial",
                      labelLimit=170, ticks=False, domain=False)

    bars = (
        alt.Chart()
        .mark_bar(size=13, cornerRadiusEnd=3)
        .encode(
            x=alt.X("count:Q",
                    axis=alt.Axis(title=None, grid=True, gridColor="#f0f0f0",
                                  tickCount=5, labelFontSize=10, domain=False)),
            y=alt.Y("label:N", sort=y_sort, axis=y_axis),
            color=alt.Color("device:N",
                            scale=color_scale,
                            legend=alt.Legend(title=None, orient="top-right",
                                              labelFontSize=12, symbolSize=110,
                                              direction="horizontal")),
            tooltip=[
                alt.Tooltip("bu:N",      title="BU"),
                alt.Tooltip("country:N", title="Country"),
                alt.Tooltip("device:N",  title="Device"),
                alt.Tooltip("count:Q",   title="Automated", format=","),
            ],
        )
    )

    # Text labels at end of each bar — transform_filter avoids clutter on zero bars
    text = (
        alt.Chart()
        .mark_text(align="left", dx=5, fontSize=9.5, color="#555555")
        .encode(
            x=alt.X("count:Q"),
            y=alt.Y("label:N", sort=y_sort),
            text=alt.Text("count:Q", format=","),
        )
        .transform_filter(alt.datum.count > 0)
    )

    chart = (
        alt.layer(bars, text, data=df)
        .properties(height=alt.Step(21))
        .facet(
            row=alt.Row(
                "bu:N",
                sort=bus,
                header=alt.Header(
                    title=None,
                    labelAngle=0,
                    labelAlign="left",
                    labelFontSize=13,
                    labelFontWeight="bold",
                    labelColor="#1a1f36",
                    labelFont="Arial",
                    labelPadding=10,
                ),
            )
        )
        .resolve_scale(y="independent", x="shared")
        .configure_view(stroke="#e8e8e8", strokeWidth=1)
        .configure_axis(labelFont="Arial")
        .configure_legend(labelFont="Arial", padding=4)
    )

    return chart, bus


# ── UI card helpers ───────────────────────────────────────────────────────────
def _fw_card(col, icon: str, name: str, subtitle: str, bg: str) -> None:
    col.markdown(
        f"""<div style="background:{bg};border:1px solid #ddd;border-radius:10px;
                    padding:14px 16px;display:flex;align-items:center;gap:12px;min-height:68px">
            <span style="font-size:26px;line-height:1">{icon}</span>
            <div>
                <div style="font-weight:700;font-size:13.5px;color:#1a1f36">{name}</div>
                <div style="font-size:11px;color:#5e6677;margin-top:2px">{subtitle}</div>
            </div>
        </div>""",
        unsafe_allow_html=True,
    )


def _metric_badge(col, value: str, label: str, sub: str = "") -> None:
    sub_html = (f'<div style="font-size:9.5px;color:#888;margin-top:1px">{sub}</div>'
                if sub else "")
    col.markdown(
        f"""<div style="background:white;border:1px solid #ddd;border-radius:10px;
                    padding:12px 14px;text-align:center;min-height:68px;
                    display:flex;flex-direction:column;justify-content:center">
            <div style="font-size:23px;font-weight:800;color:{_RED};line-height:1.1">{value}</div>
            <div style="font-size:10px;font-weight:600;color:#1a1f36;margin-top:2px">{label}</div>
            {sub_html}
        </div>""",
        unsafe_allow_html=True,
    )


# ── render ────────────────────────────────────────────────────────────────────
def render() -> None:
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

    s_tot = metrics.totals(metrics.select_smoke(web_auto))
    a_tot = metrics.totals(all_auto)

    # ── Header row: frameworks + metric badges ────────────────────────────────
    c_fw1, c_fw2, _sp, c_m1, c_m2 = st.columns([2.3, 2.3, 0.15, 1.3, 1.3])
    _fw_card(c_fw1, "☕", "Java  /  Selenium  /  Cucumber",
             "Legacy framework used by aLab", "#FFFBEC")
    _fw_card(c_fw2, "🤖", "TestIM",
             "AI powered test automation platform", "#EBF2FF")
    _metric_badge(c_m1, f"+{s_tot['total']:,}", "Test Cases", "Smoke Suite")
    _metric_badge(c_m2, f"+{a_tot['total']:,}", "Test Cases", "Total Count")

    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

    st.markdown(
        "<div style='font-family:Arial;font-weight:700;font-size:15px;"
        "color:#1a1f36;border-left:4px solid #ED7D31;padding-left:10px;margin-bottom:8px'>"
        "📊 Automated Tests by Business Unit</div>",
        unsafe_allow_html=True,
    )

    # ── Chart ─────────────────────────────────────────────────────────────────
    chart, _ = _build_chart(all_auto)
    st.altair_chart(chart, use_container_width=True)

    st.markdown(
        "<div style='font-size:10.5px;color:#888;margin-top:2px'>"
        "* These numbers are calculated with the same logic of other tabs"
        "</div>",
        unsafe_allow_html=True,
    )