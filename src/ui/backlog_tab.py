"""Backlog & Coverage tab.

For every website BU, classifies each non-deprecated case as:
  - automated      : case_id appears in evaluate_rules().automated (consistent with Explorer)
  - not_applicable : any status field contains an "Automation not applicable" value
  - backlog        : any other non-empty, non-automated status
  - unknown        : no status field populated

Metrics shown
─────────────
  Total            all non-deprecated cases in the suite
  Automated        matched by the existing rules (same count as Explorer)
  Backlog          cases that could still be automated (not N/A, not done)
  Not Applicable   cases explicitly excluded from automation
  Cov. vs Total    Automated / Total
  Cov. vs Auto.    Automated / (Automated + Backlog)   ← "how complete is the automatable set"
  N/A %            Not Applicable / (Automated + Backlog + N/A)
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ..bu_rules import ALL_RULES, WEBSITE_BUS
from ..rules_engine import evaluate_rules

# ------------------------------------------------------------------ constants
_STATUS_AUTO: set[str] = {
    "Automated", "Automated DEV", "Automated UAT", "Automated Prod",
}
_STATUS_NA: set[str] = {
    "Automation not applicable",
}

_FW_LABELS: dict[str, str] = {
    "java":           "Java",
    "testim_desktop": "TestIM Desktop",
    "testim_mobile":  "TestIM Mobile",
}

COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria",   "BE": "Belgium",  "CH": "Switzerland",
    "CZ": "Czech Rep.","FR": "France",   "GB": "United Kingdom",
    "HU": "Hungary",   "IE": "Ireland",  "IT": "Italy",
    "LT": "Lithuania", "LU": "Luxembourg","LV": "Latvia",
    "NL": "Netherlands","RO": "Romania", "SK": "Slovakia",
    "TR": "Turkey",    "UK": "United Kingdom",
}


# ------------------------------------------------------------------ logic
def _classify(raw: pd.DataFrame, auto_ids: set[int]) -> pd.Series:
    """Vectorised classification of each case row.

    Priority: automated > not_applicable > backlog > unknown
    """
    status_cols = [c for c in raw.columns if c.startswith("status_")]
    idx = raw.index

    na_mask      = pd.Series(False, index=idx)
    backlog_mask = pd.Series(False, index=idx)

    for col in status_cols:
        s = raw[col]
        na_mask      |= s.isin(_STATUS_NA)
        backlog_mask |= s.notna() & ~s.isin(_STATUS_AUTO | _STATUS_NA) & (s != "")

    cat = pd.Series("unknown", index=idx, dtype=object)
    cat[backlog_mask]             = "backlog"
    cat[na_mask]                  = "not_applicable"
    cat[raw["case_id"].isin(auto_ids)] = "automated"   # highest priority
    return cat


def _stats_from_cat(cat: pd.Series, total: int) -> dict:
    counts  = cat.value_counts()
    n_auto  = int(counts.get("automated",      0))
    n_back  = int(counts.get("backlog",         0))
    n_na    = int(counts.get("not_applicable", 0))
    automatable = n_auto + n_back
    scoped      = n_auto + n_back + n_na
    return {
        "total":            total,
        "automated":        n_auto,
        "backlog":          n_back,
        "not_applicable":   n_na,
        "cov_total":        n_auto / total        * 100 if total        else 0.0,
        "cov_automatable":  n_auto / automatable  * 100 if automatable  else 0.0,
        "na_pct":           n_na   / scoped        * 100 if scoped       else 0.0,
    }


def _load(bu: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (raw_non_deprecated, automated) for a website BU."""
    rules  = [r for r in ALL_RULES if r.bu == bu and r.scope == "website"]
    result = evaluate_rules(tuple(r.name for r in rules))
    raw    = result.raw_cases
    auto   = result.automated
    if not raw.empty:
        raw = raw[~raw["deprecated"]].reset_index(drop=True)
    return raw, auto


# ------------------------------------------------------------------ summary
@st.cache_data(show_spinner=False, ttl=3600)
def _build_summary() -> pd.DataFrame:
    rows = []
    for bu in WEBSITE_BUS:
        raw, auto = _load(bu)
        if raw.empty:
            continue
        auto_ids = set(auto["case_id"].unique()) if not auto.empty else set()
        cat = _classify(raw, auto_ids)
        s   = _stats_from_cat(cat, len(raw))
        rows.append({
            "BU":                  bu,
            "Total":               s["total"],
            "Automated":           s["automated"],
            "Backlog":             s["backlog"],
            "Not Applicable":      s["not_applicable"],
            "Cov. vs Total %":     round(s["cov_total"],       1),
            "Cov. vs Auto. %":     round(s["cov_automatable"], 1),
            "N/A %":               round(s["na_pct"],          1),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ detail
def _detail_view(bu: str) -> None:
    raw, auto = _load(bu)
    if raw.empty:
        st.info("No cases found.")
        return

    auto_ids = set(auto["case_id"].unique()) if not auto.empty else set()
    cat = _classify(raw, auto_ids)
    s   = _stats_from_cat(cat, len(raw))

    # ── Metric cards ─────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total (baseline)",  f"{s['total']:,}")
    c2.metric("Automated",         f"{s['automated']:,}",
              help=f"{s['cov_total']:.1f}% of total baseline")
    c3.metric("Backlog",           f"{s['backlog']:,}",
              help="Not yet automated — excludes N/A cases")
    c4.metric("Not Applicable",    f"{s['not_applicable']:,}",
              help=f"{s['na_pct']:.1f}% of all scoped cases")

    # ── Coverage bar ─────────────────────────────────────────────────────────
    st.markdown(
        f"**Coverage vs total baseline:** `{s['cov_total']:.1f}%` &nbsp;·&nbsp; "
        f"**Coverage vs automatable:** `{s['cov_automatable']:.1f}%`",
        unsafe_allow_html=True,
    )
    st.divider()

    # ── Automated breakdown: Country × Framework ──────────────────────────────
    if auto.empty:
        st.info("No automated cases for this BU.")
        return

    auto_dedup = auto.drop_duplicates(subset=["case_id", "country_label", "device"])

    tab_country, tab_framework = st.tabs(["By Country", "By Framework"])

    with tab_country:
        country_counts = (
            auto_dedup
            .groupby("country_label")["case_id"].nunique()
            .reset_index(name="Automated")
            .rename(columns={"country_label": "Country"})
            .sort_values("Automated", ascending=False)
        )
        country_counts["Country"] = country_counts["Country"].map(
            lambda c: COUNTRY_NAMES.get(c, c)
        )
        st.dataframe(country_counts, use_container_width=True, hide_index=True)

    with tab_framework:
        auto_dedup = auto_dedup.copy()
        auto_dedup["framework_label"] = auto_dedup["framework"].map(_FW_LABELS).fillna(
            auto_dedup["framework"]
        )
        fw_pivot = (
            auto_dedup
            .groupby(["country_label", "framework_label"])["case_id"]
            .nunique()
            .unstack(fill_value=0)
            .rename(index=lambda c: COUNTRY_NAMES.get(c, c))
        )
        fw_pivot.index.name = "Country"
        st.dataframe(fw_pivot, use_container_width=True)


# ------------------------------------------------------------------ render
def render() -> None:
    st.subheader("📋 Backlog & Coverage")
    st.caption(
        "Baseline = all non-deprecated cases · "
        "Backlog = any status except Automated\\* and Automation not applicable · "
        "Automated = consistent with Explorer tab"
    )

    # ── All-BU summary ────────────────────────────────────────────────────────
    with st.spinner("Computing backlog stats for all BUs…"):
        summary = _build_summary()

    if summary.empty:
        st.warning("No data available.")
        return

    st.markdown("#### All Business Units")
    st.dataframe(
        summary,
        use_container_width=True,
        hide_index=True,
        column_config={
            "BU":              st.column_config.TextColumn("Business Unit", width="medium"),
            "Total":           st.column_config.NumberColumn("Total"),
            "Automated":       st.column_config.NumberColumn("Automated"),
            "Backlog":         st.column_config.NumberColumn("Backlog"),
            "Not Applicable":  st.column_config.NumberColumn("N/A"),
            "Cov. vs Total %": st.column_config.NumberColumn(
                "Cov. vs Total", format="%.1f%%"),
            "Cov. vs Auto. %": st.column_config.NumberColumn(
                "Cov. vs Automatable", format="%.1f%%"),
            "N/A %":           st.column_config.NumberColumn(
                "N/A %", format="%.1f%%"),
        },
    )

    st.divider()

    # ── BU detail ────────────────────────────────────────────────────────────
    st.markdown("#### Detail by Business Unit")
    bu_choice = st.selectbox(
        "Business Unit", WEBSITE_BUS, key="bl_bu_detail",
        label_visibility="collapsed",
    )
    _detail_view(bu_choice)
