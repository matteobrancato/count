"""Report tab — clean, screenshot-ready automation coverage report.

Reproduces the 'Automation – Frameworks and Status' PowerPoint slide:
  • Header  : framework cards (Java, TestIM) + key metric badges
  • Chart   : horizontal bar chart — automated cases per BU × country × device
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import metrics
from ..bu_rules import ALL_RULES
from ..rules_engine import evaluate_rules

# ── palette ───────────────────────────────────────────────────────────────────
_BLUE       = "#4472C4"   # Mobile bars
_ORANGE     = "#ED7D31"   # Desktop bars
_RED        = "#C00000"   # metric numbers
_BG_JAVA    = "#FFFBEC"   # java card bg
_BG_TESTIM  = "#EBF2FF"   # testim card bg
_BU_SEP_COL = "rgba(0,0,0,0)"  # invisible separator bar

# Preferred display order (matches slide)
_BU_ORDER = [
    "The Perfume Shop", "Savers", "Superdrug",
    "Kruidvat", "Trekplaister", "Watsons", "Drogas",
    "Marionnaud", "ICI Paris XL",
    "Next Gen",
]


# ── data ──────────────────────────────────────────────────────────────────────
def _load() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (website_auto, all_auto), both deduplicated on (bu, country, device, case_id)."""
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
def _build_chart(auto: pd.DataFrame) -> go.Figure:
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

    # Build the y-axis row list bottom-to-top.
    # Each BU group: [country rows…, BU_separator]
    # reversed(bus) → last BU at bottom, first BU at top.
    # Within a BU: reversed(countries) + Desktop-before-Mobile
    #   → top-to-bottom reading: country A: Mobile then Desktop, country B: …
    y_labels:   list[str] = []
    x_vals:     list[int] = []
    bar_colors: list[str] = []
    bar_texts:  list[str] = []

    for bu in reversed(bus):
        bu_data   = grp[grp["bu"] == bu]
        countries = sorted(bu_data["country_label"].unique())

        for ctry in reversed(countries):          # reversed so first country ends up on top
            for device in ["Desktop", "Mobile"]:  # Mobile gets higher index → above Desktop
                row = bu_data[
                    (bu_data["country_label"] == ctry) &
                    (bu_data["device"] == device)
                ]
                count = int(row["count"].iloc[0]) if not row.empty else 0
                y_labels.append(f"  {device.lower()} {ctry}")
                x_vals.append(count)
                bar_colors.append(_ORANGE if device == "Desktop" else _BLUE)
                bar_texts.append(str(count) if count else "")

        # BU separator row: zero-width bar, bold name on y-axis
        y_labels.append(f"<b>  {bu}</b>")
        x_vals.append(0)
        bar_colors.append(_BU_SEP_COL)
        bar_texts.append("")

    # ── traces ────────────────────────────────────────────────────────────────
    fig = go.Figure()

    # Main bar trace (single trace with per-bar colours)
    fig.add_trace(go.Bar(
        y=y_labels,
        x=x_vals,
        orientation="h",
        marker=dict(color=bar_colors, line=dict(width=0)),
        text=bar_texts,
        textposition="outside",
        textfont=dict(size=9.5, color="#444444"),
        cliponaxis=False,
        showlegend=False,
        hovertemplate="%{y}: <b>%{x:,}</b><extra></extra>",
    ))

    # Legend-only dummy traces
    for label, color in [("Mobile", _BLUE), ("Desktop", _ORANGE)]:
        fig.add_trace(go.Bar(
            y=[None], x=[None], orientation="h",
            name=label, marker_color=color, showlegend=True,
        ))

    # ── horizontal separator lines between BU groups ──────────────────────────
    # BU separator rows are at specific y-label values; draw a line just below them.
    sep_labels = {lbl for lbl in y_labels if lbl.startswith("<b>")}
    shapes = [
        dict(
            type="line", xref="paper", yref="y",
            x0=0, x1=1.0,
            y0=lbl, y1=lbl,
            line=dict(color="#e0e0e0", width=1.2),
        )
        for lbl in sep_labels
    ]

    # ── layout ────────────────────────────────────────────────────────────────
    n_rows = len(y_labels)
    fig.update_layout(
        height=max(450, n_rows * 19 + 60),
        margin=dict(l=20, r=80, t=4, b=30),
        plot_bgcolor="white",
        paper_bgcolor="white",
        barmode="overlay",
        shapes=shapes,
        legend=dict(
            orientation="h",
            yanchor="bottom", y=1.0,
            xanchor="right",  x=1.0,
            font=dict(size=12, family="Arial"),
            traceorder="reversed",
        ),
        xaxis=dict(
            showgrid=True, gridcolor="#f3f3f3",
            zeroline=True, zerolinecolor="#dddddd", zerolinewidth=1,
            showline=False, title=None,
            tickfont=dict(size=10),
        ),
        yaxis=dict(
            automargin=True,
            tickfont=dict(size=10.5, family="Arial"),
            showline=False, showgrid=False, showticklabels=True,
        ),
        font=dict(family="Arial, sans-serif", color="#333333"),
        hoverlabel=dict(bgcolor="white", font_size=12),
    )
    return fig


# ── UI card helpers ───────────────────────────────────────────────────────────
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
    # Tighter padding for clean screenshots
    st.markdown(
        "<style>.block-container{padding-top:1rem;padding-bottom:0.5rem}</style>",
        unsafe_allow_html=True,
    )

    # ── Title ─────────────────────────────────────────────────────────────────
    st.markdown(
        "<h2 style='text-align:center;font-family:Arial;font-weight:800;"
        "color:#1a1f36;margin-bottom:20px;letter-spacing:-0.5px'>"
        "Automation &nbsp;·&nbsp; Frameworks and Status</h2>",
        unsafe_allow_html=True,
    )

    # ── Load data ─────────────────────────────────────────────────────────────
    with st.spinner("Loading…"):
        web_auto, all_auto = _load()

    if all_auto.empty:
        st.warning("No automated data available. Click 🔄 Refresh.")
        return

    smoke  = metrics.select_smoke(web_auto)
    s_tot  = metrics.totals(smoke)
    a_tot  = metrics.totals(all_auto)

    # ── Header row: framework cards + metrics ─────────────────────────────────
    c_fw1, c_fw2, _sp, c_m1, c_m2 = st.columns([2.3, 2.3, 0.15, 1.3, 1.3])

    _fw_card(c_fw1, "☕",
             "Java  /  Selenium  /  Cucumber",
             "Legacy framework used by aLab",
             _BG_JAVA)
    _fw_card(c_fw2, "🤖",
             "TestIM",
             "AI powered test automation platform",
             _BG_TESTIM)
    _metric_badge(c_m1,
                  f"+{s_tot['total']:,}",
                  "Test Cases",
                  "Smoke Suite")
    _metric_badge(c_m2,
                  f"+{a_tot['total']:,}",
                  "Test Cases",
                  "Total Count")

    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)

    # ── Section header ────────────────────────────────────────────────────────
    st.markdown(
        "<div style='font-family:Arial;font-weight:700;font-size:15px;"
        "color:#1a1f36;border-left:4px solid #ED7D31;"
        "padding-left:10px;margin-bottom:6px'>"
        "📊 Automated Tests by Business Unit *</div>",
        unsafe_allow_html=True,
    )

    # ── Chart ─────────────────────────────────────────────────────────────────
    fig = _build_chart(all_auto)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    # ── Footer note ───────────────────────────────────────────────────────────
    st.markdown(
        "<div style='font-size:10.5px;color:#888;margin-top:4px'>"
        "* Expanded count: one row per test case × country × device — "
        "deduplicated within each (BU, country, device) combination."
        "</div>",
        unsafe_allow_html=True,
    )
