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
    "Kruidvat", "Trekpleister", "Watsons", "Drogas",
    "Marionnaud", "ICI Paris XL", "Next Gen",
]


# ── data loading (shares evaluate_rules cache with other tabs) ────────────────
def _add_regression_flag(auto: pd.DataFrame, raw: pd.DataFrame) -> pd.DataFrame:
    """Add `is_regression` from the Backlog tab's own baseline expansion.

    A row is regression when its (case, country, device) is an *automated*
    baseline row per the Backlog method (`big_regr` labels × multi_countries) —
    the single source of truth, so the solid segments reconcile with the
    Backlog / Coverage baseline numbers.  Unspecified-device rows (Next Gen)
    fall back to case-level membership since the baseline is device-labelled.
    """
    if auto.empty:
        return auto
    try:
        from . import backlog_tab as bl
        _, expanded_by_bu, _ = bl._backlog_data()
        frames = [f[f["category"] == "automated"] for f in expanded_by_bu.values()]
        base = (pd.concat(frames, ignore_index=True) if frames
                else pd.DataFrame(columns=["case_id", "country_label", "device"]))
    except Exception:                                                   # noqa: BLE001
        base = pd.DataFrame(columns=["case_id", "country_label", "device"])
    if base.empty:
        return auto.assign(is_regression=False)

    keys = set(zip(base["case_id"].astype(int),
                   base["country_label"], base["device"]))
    case_any = set(base["case_id"].astype(int))
    out = auto.copy()
    out["is_regression"] = [
        (cid, ctry, dev) in keys or (dev == "Unspecified" and cid in case_any)
        for cid, ctry, dev in zip(out["case_id"].astype(int),
                                  out["country_label"], out["device"])
    ]
    return out


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
    grp["dev_rank"]  = grp["device"].map(
        {"Mobile": 0, "Desktop": 1, "Unspecified": 2, "API": 2}).fillna(2).astype(int)
    grp["sort_key"]  = grp["ctry_rank"] * 10 + grp["dev_rank"]
    # For device-less rows (Next Gen "API" / Unspecified) show just the country
    # code — no device prefix.
    grp["label"] = grp.apply(
        lambda r: r["country_label"] if r["device"] in ("Unspecified", "API")
        else r["device"].lower() + " " + r["country_label"],
        axis=1,
    )
    grp["bu_rank"] = grp["bu"].map({b: i for i, b in enumerate(bus)})

    return grp.rename(columns={"country_label": "country"})


def _ordered_bus(present: set[str]) -> list[str]:
    bus = [b for b in _BU_ORDER if b in present]
    return bus + sorted(b for b in present if b not in bus)


def _build_bu_chart(df_bu: pd.DataFrame) -> alt.LayerChart:
    """One responsive chart for a single BU (stacked bars + total labels).

    One chart per BU rendered inside `st.columns` (instead of a fixed-width
    Altair facet grid) so every panel is container-width-aware — the Report was
    the app's only non-responsive chart.
    Solid segment = regression baseline, faded = other automated.
    """
    y_sort = alt.EncodingSortField(field="sort_key", order="ascending")

    color_scale = alt.Scale(
        domain=["Mobile", "Desktop", "Unspecified"],
        range=[_BLUE, _ORANGE, _GREY],
    )

    y_axis = alt.Axis(title=None, labelFontSize=11, labelFont="Inter",
                      labelColor=COLORS["text"],
                      labelLimit=170, ticks=False, domain=False)

    bars = (
        alt.Chart()
        .mark_bar(size=13, cornerRadiusEnd=3)
        .encode(
            x=alt.X("count:Q",
                    stack="zero",
                    axis=alt.Axis(title=None, grid=True, gridColor=COLORS["grid"],
                                  tickCount=5, labelFontSize=11, domain=False,
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
        .mark_text(align="left", dx=5, fontSize=11, color=COLORS["text"])
        .encode(
            x=alt.X("total:Q"),
            y=alt.Y("label:N", sort=y_sort),
            text=alt.Text("total:Q", format=","),
        )
        .transform_filter(alt.datum.total > 0)
    )

    return (
        alt.layer(bars, text, data=df_bu)
        .properties(height=alt.Step(21), background="transparent")
        .configure_view(stroke=None, fill="transparent")
        .configure_axis(labelFont="Inter")
    )


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
    sub_html = (f'<div style="font-size:11px;color:{COLORS["muted"]};margin-top:1px">{sub}</div>'
                if sub else "")
    col.markdown(
        f"""<div style="background:{COLORS['surface']};border:1px solid {COLORS['border']};
                    border-radius:14px;padding:12px 14px;text-align:center;min-height:70px;
                    box-shadow:0 1px 2px rgba(15,23,42,0.04);
                    display:flex;flex-direction:column;justify-content:center">
            <div style="font-size:23px;font-weight:800;color:{COLORS['brand']};line-height:1.1">{value}</div>
            <div style="font-size:11px;font-weight:600;color:{COLORS['ink']};margin-top:2px;
                        text-transform:uppercase;letter-spacing:0.04em">{label}</div>
            {sub_html}
        </div>""",
        unsafe_allow_html=True,
    )


# ── render ────────────────────────────────────────────────────────────────────
@st.fragment
def render() -> None:
    # Standard tab opener (subheader + caption) — same pattern as every other tab.
    # Section title removed (redundant with the "Report" tab label).
    st.caption(
        "Automated tests per Business Unit, split by framework and device. "
        "Solid bar segments are the regression baseline; faded segments are "
        "other automated tests."
    )

    with st.spinner("Loading…"):
        web_auto, all_auto = _load()

    if all_auto.empty:
        st.warning("No automated data available — data refreshes automatically every few hours (or use the ↻ next to the tabs).")
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

    # ── Charts — one responsive panel per BU, in aligned 2-column rows ────────
    bus = _ordered_bus(set(all_auto["bu"].unique()))
    df  = _prepare_chart_data(all_auto, bus)
    for row_start in range(0, len(bus), 2):
        cols = st.columns(2, gap="large")
        for col, bu in zip(cols, bus[row_start:row_start + 2]):
            with col:
                st.markdown(
                    f"<div style='font-weight:700;font-size:13px;"
                    f"color:{COLORS['ink']};margin:8px 0 2px'>{bu}</div>",
                    unsafe_allow_html=True,
                )
                st.altair_chart(_build_bu_chart(df[df["bu"] == bu]),
                                width="stretch")

    st.markdown(
        f"<div style='font-size:11px;color:{COLORS['muted']};margin-top:2px'>"
        f"* Solid regression segments use the same baseline as the Backlog tab"
        f"</div>",
        unsafe_allow_html=True,
    )