from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from .. import metrics
from ..bu_rules import ALL_RULES
from ..rules_engine import evaluate_rules
from .styles import COLORS

# ── palette (sourced from the global design tokens) ─────────────────────────────
_BLUE   = COLORS["mobile"]       # Mobile
_ORANGE = COLORS["desktop"]      # Desktop
_GREY   = COLORS["unspecified"]  # Unspecified (Next Gen)

_BU_ORDER = [
    "The Perfume Shop", "Savers", "Superdrug",
    "Kruidvat", "Trekplaister", "Watsons", "Drogas",
    "Marionnaud", "ICI Paris XL", "Next Gen",
]


# ── data loading (shares evaluate_rules cache with other tabs) ────────────────
def _add_regression_flag(auto: pd.DataFrame, raw: pd.DataFrame) -> pd.DataFrame:
    """Add `is_regression` column based on big_regr_* labels matched to row's device."""
    if auto.empty:
        return auto
    if raw.empty or "labels" not in raw.columns:
        return auto.assign(is_regression=False)

    case_labels = raw[["case_id", "labels"]].drop_duplicates(subset=["case_id"]).copy()
    case_labels["has_desk"] = case_labels["labels"].apply(
        lambda ls: "big_regr_desktop" in ls if isinstance(ls, list) else False
    )
    case_labels["has_mob"] = case_labels["labels"].apply(
        lambda ls: "big_regr_mobile" in ls if isinstance(ls, list) else False
    )
    out  = auto.merge(case_labels[["case_id", "has_desk", "has_mob"]],
                      on="case_id", how="left")
    desk = out["has_desk"].fillna(False)
    mob  = out["has_mob"].fillna(False)
    out["is_regression"] = (
        ((out["device"] == "Desktop") & desk) |
        ((out["device"] == "Mobile")  & mob)  |
        ((out["device"] == "Unspecified") & (desk | mob))
    )
    return out.drop(columns=["has_desk", "has_mob"])


def _load() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (website_auto, all_auto) deduplicated on (bu, country, device, case_id).

    Each frame carries an `is_regression` boolean derived from big_regr_* labels,
    so the chart can stack regression vs other automated cases.
    """
    frames_all: list[pd.DataFrame] = []
    frames_web: list[pd.DataFrame] = []

    for scope in ("website", "next_gen"):
        rules = [r for r in ALL_RULES if r.scope == scope]
        if not rules:
            continue
        result = evaluate_rules(tuple(r.name for r in rules))
        if result.automated.empty:
            continue
        auto = _add_regression_flag(result.automated, result.raw_cases)
        frames_all.append(auto)
        if scope == "website":
            frames_web.append(auto)

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
    """Aggregate and annotate data for the Altair chart, split by regression flag.

    One row per (bu, country, device, category) where category ∈ {Regression, Other}.
    A 'total' column is set on exactly one row per (bu, country, device) so that
    the text mark renders the count once at the end of each stacked bar (the other
    rows carry total=0 and are removed via transform_filter).
    """
    if "is_regression" not in auto.columns:
        auto = auto.assign(is_regression=False)

    grp = (
        auto.groupby(["bu", "country_label", "device", "is_regression"])["case_id"]
        .nunique()
        .reset_index(name="count")
    )
    grp["category"]      = grp["is_regression"].map({True: "Regression", False: "Other"}).fillna("Other")
    grp["category_rank"] = grp["category"].map({"Regression": 0, "Other": 1}).astype(int)

    # One row per group carries the total — used by the text layer.
    totals_per_group = grp.groupby(["bu", "country_label", "device"])["count"].transform("sum")
    is_first         = ~grp.duplicated(subset=["bu", "country_label", "device"], keep="first")
    grp["total"]     = totals_per_group.where(is_first, 0)

    # Sort country alphabetically per BU
    grp["ctry_rank"] = (
        grp.groupby("bu")["country_label"]
        .transform(lambda s: s.map({c: i for i, c in enumerate(sorted(s.unique()))}))
    )
    grp["dev_rank"]  = grp["device"].map({"Mobile": 0, "Desktop": 1, "Unspecified": 2}).fillna(2).astype(int)
    grp["sort_key"]  = grp["ctry_rank"] * 10 + grp["dev_rank"]
    # For Unspecified device (Next Gen), show just the country code — no device prefix
    grp["label"] = grp.apply(
        lambda r: r["country_label"] if r["device"] == "Unspecified"
        else r["device"].lower() + " " + r["country_label"],
        axis=1,
    )
    grp["bu_rank"] = grp["bu"].map({b: i for i, b in enumerate(bus)})

    return grp.rename(columns={"country_label": "country"})


def _build_chart(auto: pd.DataFrame) -> tuple[alt.Chart, list[str]]:
    """Return (faceted Altair chart, ordered BU list).

    Each bar is stacked: solid segment = regression baseline, faded = other.
    Total count is shown as text at the end of the stacked bar.
    """
    present = set(auto["bu"].unique())
    bus = [b for b in _BU_ORDER if b in present]
    bus += sorted(b for b in present if b not in bus)

    df = _prepare_chart_data(auto, bus)

    # Sort y-axis per-facet by sort_key (field-based, no global list).
    y_sort = alt.EncodingSortField(field="sort_key", order="ascending")

    color_scale = alt.Scale(
        domain=["Mobile", "Desktop", "Unspecified"],
        range=[_BLUE, _ORANGE, _GREY],
    )

    y_axis = alt.Axis(title=None, labelFontSize=10.5, labelFont="Inter",
                      labelColor=COLORS["text"],
                      labelLimit=170, ticks=False, domain=False)

    bars = (
        alt.Chart()
        .mark_bar(size=13, cornerRadiusEnd=3)
        .encode(
            x=alt.X("count:Q",
                    stack="zero",
                    axis=alt.Axis(title=None, grid=True, gridColor=COLORS["grid"],
                                  tickCount=5, labelFontSize=10, domain=False,
                                  labelColor=COLORS["muted"])),
            y=alt.Y("label:N", sort=y_sort, axis=y_axis),
            color=alt.Color("device:N", scale=color_scale, legend=None),
            opacity=alt.Opacity(
                "category:N",
                scale=alt.Scale(domain=["Regression", "Other"], range=[1.0, 0.40]),
                legend=None,
            ),
            order=alt.Order("category_rank:Q", sort="ascending"),
            tooltip=[
                alt.Tooltip("bu:N",       title="BU"),
                alt.Tooltip("country:N",  title="Country"),
                alt.Tooltip("device:N",   title="Device"),
                alt.Tooltip("category:N", title="Type"),
                alt.Tooltip("count:Q",    title="Count", format=","),
            ],
        )
    )

    # Text label at end of each stacked bar — only the row with total>0 renders.
    text = (
        alt.Chart()
        .mark_text(align="left", dx=5, fontSize=9.5, color=COLORS["muted"])
        .encode(
            x=alt.X("total:Q"),
            y=alt.Y("label:N", sort=y_sort),
            text=alt.Text("total:Q", format=","),
        )
        .transform_filter(alt.datum.total > 0)
    )

    chart = (
        alt.layer(bars, text, data=df)
        .properties(height=alt.Step(21))
        .facet(
            facet=alt.Facet(
                "bu:N",
                sort=bus,
                header=alt.Header(
                    title=None,
                    labelAngle=0,
                    labelAlign="left",
                    labelFontSize=13,
                    labelFontWeight="bold",
                    labelColor=COLORS["ink"],
                    labelFont="Inter",
                    labelPadding=10,
                ),
            ),
            columns=2,
        )
        .properties(background="transparent")   # blend into the page canvas
        .resolve_scale(y="independent", x="shared")
        .configure_view(stroke=COLORS["border"], strokeWidth=1, fill="transparent")
        .configure_axis(labelFont="Inter")
        .configure_legend(labelFont="Inter", padding=4)
    )

    return chart, bus


# ── UI card helpers ───────────────────────────────────────────────────────────
def _fw_card(col, icon: str, name: str, subtitle: str, bg: str) -> None:
    col.markdown(
        f"""<div style="background:{bg};border:1px solid {COLORS['border']};border-radius:14px;
                    padding:15px 18px;display:flex;align-items:center;gap:13px;min-height:70px;
                    box-shadow:0 1px 2px rgba(15,23,42,0.04)">
            <span style="font-size:26px;line-height:1">{icon}</span>
            <div>
                <div style="font-weight:700;font-size:13.5px;color:{COLORS['ink']}">{name}</div>
                <div style="font-size:11px;color:{COLORS['muted']};margin-top:2px">{subtitle}</div>
            </div>
        </div>""",
        unsafe_allow_html=True,
    )


def _metric_badge(col, value: str, label: str, sub: str = "") -> None:
    sub_html = (f'<div style="font-size:10px;color:{COLORS["muted"]};margin-top:1px">{sub}</div>'
                if sub else "")
    col.markdown(
        f"""<div style="background:{COLORS['surface']};border:1px solid {COLORS['border']};
                    border-radius:14px;padding:12px 14px;text-align:center;min-height:70px;
                    box-shadow:0 1px 2px rgba(15,23,42,0.04);
                    display:flex;flex-direction:column;justify-content:center">
            <div style="font-size:23px;font-weight:800;color:{COLORS['brand']};line-height:1.1">{value}</div>
            <div style="font-size:10px;font-weight:600;color:{COLORS['ink']};margin-top:2px;
                        text-transform:uppercase;letter-spacing:0.04em">{label}</div>
            {sub_html}
        </div>""",
        unsafe_allow_html=True,
    )


# ── render ────────────────────────────────────────────────────────────────────
def render() -> None:
    st.markdown(
        f"<h2 style='text-align:center;font-weight:800;"
        f"color:{COLORS['ink']};margin-bottom:20px;letter-spacing:-0.02em'>"
        f"Automation &nbsp;·&nbsp; Frameworks and Status</h2>",
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
             "Legacy framework used by aLab", COLORS["java_bg"])
    _fw_card(c_fw2, "🤖", "TestIM",
             "AI powered test automation platform", COLORS["testim_bg"])
    _metric_badge(c_m1, f"+{s_tot['total']:,}", "Test Cases", "Smoke Suite")
    _metric_badge(c_m2, f"+{a_tot['total']:,}", "Test Cases", "Total Count")

    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

    # ── Section header + legend on the same row ───────────────────────────────
    def _dot(color: str) -> str:
        return (f'<span style="display:inline-block;width:11px;height:11px;'
                f'border-radius:2px;background:{color};margin-right:5px;'
                f'vertical-align:middle"></span>')

    legend_html = (
        f'<div style="display:flex;align-items:center;gap:16px;'
        f'font-size:12px;color:{COLORS["text"]}">'
        f'{_dot(_BLUE)}<span>Mobile</span>'
        f'{_dot(_ORANGE)}<span>Desktop</span>'
        f'{_dot(_GREY)}<span style="color:{COLORS["muted"]}">Unspecified</span>'
        f'<span style="color:{COLORS["muted"]};font-size:11px;margin-left:6px;'
        f'border-left:1px solid {COLORS["border"]};padding-left:10px">'
        f'solid = regression&nbsp;·&nbsp;faded = other</span>'
        f'</div>'
    )
    st.markdown(
        f'<div style="display:flex;align-items:center;justify-content:space-between;'
        f'margin-bottom:10px">'
        f'<div style="font-weight:700;font-size:15px;color:{COLORS["ink"]};'
        f'border-left:3px solid {COLORS["brand"]};padding-left:10px">'
        f'📊 Automated Tests by Business Unit</div>'
        f'{legend_html}'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Chart ─────────────────────────────────────────────────────────────────
    chart, _ = _build_chart(all_auto)
    st.altair_chart(chart, use_container_width=True)

    st.markdown(
        f"<div style='font-size:10.5px;color:{COLORS['muted']};margin-top:2px'>"
        f"* These numbers are calculated with the same logic of other tabs"
        f"</div>",
        unsafe_allow_html=True,
    )