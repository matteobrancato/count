from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import metrics
from ..bu_rules import ALL_RULES
from ..rules_engine import evaluate_rules
from .styles import COLORS


# --------------------------------------------------------------------- helpers
def _bu_country_map(scope: str) -> dict[str, list[str]]:
    """Return {bu: [country labels]} for rules matching a scope."""
    out: dict[str, set[str]] = {}
    for r in ALL_RULES:
        if r.scope != scope:
            continue
        out.setdefault(r.bu, set())
        if r.country_labels:
            out[r.bu].update(r.country_labels.values())
        elif r.implicit_country:
            out[r.bu].add(r.implicit_country)
        else:
            out[r.bu].add(r.bu)
    return {bu: sorted(cs) for bu, cs in sorted(out.items())}


def _bu_country_picker(tree: dict[str, list[str]], key_prefix: str) -> dict[str, list[str]]:
    """Render a nested checkbox tree. Returns {bu: [selected countries]}."""
    selected: dict[str, list[str]] = {}
    for bu, countries in tree.items():
        with st.container():
            c1, c2 = st.columns([0.5, 3])
            # Real label (collapsed visually) = accessible name for screen
            # readers, and no Streamlit empty-label warning.
            checked = c1.checkbox(bu, value=True, key=f"{key_prefix}_bu_{bu}",
                                  label_visibility="collapsed")
            c2.markdown(f"**{bu}**")
            if not checked:
                continue
            if len(countries) <= 1:
                selected[bu] = countries
                continue
            with st.expander(f"{len(countries)} countries", expanded=False):
                picks = []
                for ctry in countries:
                    if st.checkbox(ctry, value=True, key=f"{key_prefix}_{bu}_{ctry}"):
                        picks.append(ctry)
                selected[bu] = picks
    return selected


def _apply_selection(df: pd.DataFrame, selection: dict[str, list[str]]) -> pd.DataFrame:
    if df.empty or not selection:
        return df.iloc[0:0]
    masks = []
    for bu, countries in selection.items():
        if not countries:
            continue
        masks.append((df["bu"] == bu) & (df["country_label"].isin(countries)))
    if not masks:
        return df.iloc[0:0]
    combined = masks[0]
    for m in masks[1:]:
        combined = combined | m
    return df[combined]


# --------------------------------------------------------------------- cards
def _metric_card(title: str, subset: pd.DataFrame, accent: str,
                 tooltip: str = "") -> None:
    tot = metrics.totals(subset)
    tip = f' title="{tooltip}"' if tooltip else ""
    st.markdown(
        f"""
        <div{tip} style="
            padding:18px 22px;border-radius:16px;
            background:linear-gradient(135deg,{accent}1f,{accent}08);
            border:1px solid {accent}3a;margin-bottom:8px;
            box-shadow:0 1px 3px rgba(15,23,42,0.05)">
            <div style="font-size:12.5px;color:{COLORS['muted']};text-transform:uppercase;
                        letter-spacing:0.07em;font-weight:700">{title}</div>
            <div style="font-size:36px;font-weight:800;color:{COLORS['ink']};margin-top:4px;
                        letter-spacing:-0.02em;line-height:1.05">
                {tot['total']:,}
            </div>
            <div style="font-size:13px;color:{COLORS['muted']};margin-top:6px">
                🖥 Desktop <b style="color:{COLORS['text']}">{tot['desktop']:,}</b>
                &nbsp;·&nbsp; 📱 Mobile <b style="color:{COLORS['text']}">{tot['mobile']:,}</b>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.expander("Breakdown", expanded=True):
        t1, t2, t3 = st.tabs(["By BU", "By Country", "By Device"])
        with t1:
            st.dataframe(metrics.breakdown_by(subset, ["bu"]),
                         width="stretch", hide_index=True)
        with t2:
            st.dataframe(metrics.breakdown_by(subset, ["bu", "country_label"]),
                         width="stretch", hide_index=True)
        with t3:
            st.dataframe(metrics.breakdown_by(subset, ["bu", "country_label", "device"]),
                         width="stretch", hide_index=True)


# --------------------------------------------------------------------- render
@st.fragment
def render() -> None:
    # Section title removed (redundant with the "Overview" tab label).
    st.caption(
        "Scope-wide automated counts (Smoke · Regression · Production Sanity). "
        "For coverage broken down by area, see the **📐 Coverage** tab."
    )

    # Build the combined automated frame for the 3 cards (Website scope only — smoke /
    # regression / sanity metrics are about website automation per the PDF).
    website_rules = [r for r in ALL_RULES if r.scope == "website"]
    result = evaluate_rules(tuple(r.name for r in website_rules))
    automated_all = result.automated

    left, right = st.columns([1, 3], gap="large")
    with left:
        st.markdown("##### BU filter")
        tree = _bu_country_map("website")
        selection = _bu_country_picker(tree, key_prefix="ov")

    automated = _apply_selection(automated_all, selection)

    with right:
        smoke = metrics.select_smoke(automated)
        regr = metrics.select_regression(automated)
        sanity = metrics.select_prod_sanity(automated)
        c1, c2, c3 = st.columns(3)
        with c1:
            _metric_card("Smoke (Highest automated)", smoke, COLORS["warning"])
        with c2:
            _metric_card(
                "No-Regression (All automated)", regr, COLORS["brand"],
                tooltip=("ALL automated cases (deduped) — NOT the big_regr "
                         "regression baseline. For the baseline figures see "
                         "the Backlog tab."),
            )
        with c3:
            _metric_card("Production Sanity", sanity, COLORS["success"])
