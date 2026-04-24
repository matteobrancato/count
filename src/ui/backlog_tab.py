"""Backlog & Coverage tab.

All counts use the same expansion as the Explorer/Pivot tab:
  one row per (case_id × country_label × device).

Classification priority (per expanded row):
  automated      → (case_id, country_label, device) is in evaluate_rules().automated
  not_applicable → any status field = "Automation not applicable"
  backlog        → any other non-empty, non-automated status
  unknown        → no status field populated

Metrics
───────
  Total            expanded (case × country × device) rows, non-deprecated, in scope
  Automated        rows matched by the existing rules — same count as Explorer pivot Total
  Backlog          rows not yet automated, excluding N/A
  Not Applicable   rows explicitly excluded from automation
  Cov. vs Total    Automated / Total
  Cov. vs Auto.    Automated / (Automated + Backlog)
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
    "AT": "Austria",    "BE": "Belgium",     "CH": "Switzerland",
    "CZ": "Czech Rep.", "FR": "France",      "GB": "United Kingdom",
    "HU": "Hungary",    "IE": "Ireland",     "IT": "Italy",
    "LT": "Lithuania",  "LU": "Luxembourg",  "LV": "Latvia",
    "NL": "Netherlands","RO": "Romania",     "SK": "Slovakia",
    "TR": "Turkey",     "UK": "United Kingdom",
}

# How the Device field value (already resolved in raw_cases) maps to expanded devices
_DEVICE_EXPAND: dict[str, list[str]] = {
    "Both":         ["Desktop", "Mobile"],
    "Desktop":      ["Desktop"],
    "Desktop only": ["Desktop"],
    "Mobile":       ["Mobile"],
    "Mobile only":  ["Mobile"],
}


# ------------------------------------------------------------------ expansion
def _expand_raw(raw: pd.DataFrame, rules: list) -> pd.DataFrame:
    """Expand raw cases by country × device, mirroring evaluate_rules expansion.

    Returns a DataFrame with columns:
        case_id, country_label, device, _cat_base
    where _cat_base is the status classification BEFORE checking the auto set.
    """
    # Build token → label map from all rules for this BU
    token_label: dict[str, str] = {}
    for rule in rules:
        for tok in rule.countries_filter:
            token_label[tok] = rule.country_labels.get(tok, tok)

    all_tokens = set(token_label)
    status_cols = [c for c in raw.columns if c.startswith("status_")]

    # ── Classify each raw case (before country/device expansion) ────────────
    na_mask      = pd.Series(False, index=raw.index)
    backlog_mask = pd.Series(False, index=raw.index)
    for col in status_cols:
        s = raw[col]
        na_mask      |= s.isin(_STATUS_NA)
        backlog_mask |= s.notna() & ~s.isin(_STATUS_AUTO | _STATUS_NA) & (s != "")

    raw = raw.copy()
    raw["_cat_base"] = "unknown"
    raw.loc[backlog_mask, "_cat_base"] = "backlog"
    raw.loc[na_mask,      "_cat_base"] = "not_applicable"

    # ── Country expansion via multi_countries ─────────────────────────────────
    if all_tokens:
        raw["_mc_filtered"] = raw["multi_countries"].apply(
            lambda mc: list({
                token_label[t]          # map to ISO label
                for t in (mc if isinstance(mc, list) else [])
                if t in all_tokens
            })
        )
        raw = raw[raw["_mc_filtered"].map(len) > 0]
    else:
        raw["_mc_filtered"] = raw.apply(lambda _: ["__ALL__"], axis=1)

    raw = raw.explode("_mc_filtered").rename(columns={"_mc_filtered": "country_label"})

    # ── Device expansion ──────────────────────────────────────────────────────
    raw["_devices"] = raw["device"].map(lambda d: _DEVICE_EXPAND.get(d, ["Unspecified"]))
    raw = raw.explode("_devices").rename(columns={"_devices": "device_exp"})

    # ── Dedup on (case_id, country_label, device) ─────────────────────────────
    expanded = (
        raw[["case_id", "country_label", "device_exp", "_cat_base"]]
        .drop_duplicates(subset=["case_id", "country_label", "device_exp"])
        .rename(columns={"device_exp": "device"})
        .reset_index(drop=True)
    )
    return expanded


def _classify_expanded(expanded: pd.DataFrame, auto: pd.DataFrame) -> pd.DataFrame:
    """Add final 'category' column, overriding _cat_base with 'automated' where applicable."""
    if auto.empty:
        expanded = expanded.copy()
        expanded["category"] = expanded["_cat_base"]
        return expanded

    auto_keys = set(
        zip(
            auto["case_id"].astype(int),
            auto["country_label"],
            auto["device"],
        )
    )
    expanded = expanded.copy()
    is_auto = expanded.apply(
        lambda r: (int(r["case_id"]), r["country_label"], r["device"]) in auto_keys,
        axis=1,
    )
    expanded["category"] = expanded["_cat_base"]
    expanded.loc[is_auto, "category"] = "automated"
    return expanded


def _stats(expanded: pd.DataFrame) -> dict:
    counts      = expanded["category"].value_counts()
    total       = len(expanded)
    n_auto      = int(counts.get("automated",      0))
    n_back      = int(counts.get("backlog",         0))
    n_na        = int(counts.get("not_applicable", 0))
    automatable = n_auto + n_back
    scoped      = n_auto + n_back + n_na
    return {
        "total":           total,
        "automated":       n_auto,
        "backlog":         n_back,
        "not_applicable":  n_na,
        "cov_total":       n_auto / total        * 100 if total        else 0.0,
        "cov_automatable": n_auto / automatable  * 100 if automatable  else 0.0,
        "na_pct":          n_na   / scoped        * 100 if scoped       else 0.0,
    }


# ------------------------------------------------------------------ load
def _load(bu: str) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    """Return (raw_non_deprecated, automated, rules) for a website BU."""
    rules  = [r for r in ALL_RULES if r.bu == bu and r.scope == "website"]
    result = evaluate_rules(tuple(r.name for r in rules))
    raw    = result.raw_cases
    auto   = result.automated
    if not raw.empty:
        raw = raw[~raw["deprecated"]].reset_index(drop=True)
    return raw, auto, rules


# ------------------------------------------------------------------ summary
def _build_summary() -> pd.DataFrame:
    rows = []
    for bu in WEBSITE_BUS:
        raw, auto, rules = _load(bu)
        if raw.empty:
            continue
        expanded  = _expand_raw(raw, rules)
        expanded  = _classify_expanded(expanded, auto)
        s         = _stats(expanded)
        rows.append({
            "BU":              bu,
            "Total":           s["total"],
            "Automated":       s["automated"],
            "Backlog":         s["backlog"],
            "Not Applicable":  s["not_applicable"],
            "Cov. vs Total %": round(s["cov_total"],       1),
            "Cov. vs Auto. %": round(s["cov_automatable"], 1),
            "N/A %":           round(s["na_pct"],          1),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------ detail
def _detail_view(bu: str) -> None:
    raw, auto, rules = _load(bu)
    if raw.empty:
        st.info("No cases found.")
        return

    expanded = _expand_raw(raw, rules)
    expanded = _classify_expanded(expanded, auto)
    s        = _stats(expanded)

    # ── Metric cards ──────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total (baseline)",  f"{s['total']:,}",
              help="Expanded rows: case × country × device")
    c2.metric("Automated",         f"{s['automated']:,}",
              help=f"{s['cov_total']:.1f}% of total — same counting as Explorer")
    c3.metric("Backlog",           f"{s['backlog']:,}",
              help="Not yet automated — excludes N/A")
    c4.metric("Not Applicable",    f"{s['not_applicable']:,}",
              help=f"{s['na_pct']:.1f}% of all scoped rows")

    st.markdown(
        f"**Coverage vs total:** `{s['cov_total']:.1f}%` &nbsp;·&nbsp; "
        f"**Coverage vs automatable:** `{s['cov_automatable']:.1f}%`",
        unsafe_allow_html=True,
    )
    st.divider()

    # ── Automated breakdown ────────────────────────────────────────────────────
    auto_rows = expanded[expanded["category"] == "automated"]
    if auto_rows.empty:
        st.info("No automated cases for this BU.")
        return

    tab_country, tab_framework = st.tabs(["By Country", "By Framework"])

    with tab_country:
        country_counts = (
            auto_rows.groupby("country_label").size()
            .reset_index(name="Automated")
            .rename(columns={"country_label": "Country"})
            .sort_values("Automated", ascending=False)
        )
        country_counts["Country"] = country_counts["Country"].map(
            lambda c: COUNTRY_NAMES.get(c, c)
        )
        st.dataframe(country_counts, use_container_width=True, hide_index=True)

    with tab_framework:
        if not auto.empty:
            auto_dedup = auto.drop_duplicates(["case_id", "country_label", "device"]).copy()
            auto_dedup["framework_label"] = (
                auto_dedup["framework"].map(_FW_LABELS).fillna(auto_dedup["framework"])
            )
            fw_pivot = (
                auto_dedup
                .groupby(["country_label", "framework_label"]).size()
                .unstack(fill_value=0)
                .rename(index=lambda c: COUNTRY_NAMES.get(c, c))
            )
            fw_pivot.index.name = "Country"
            st.dataframe(fw_pivot, use_container_width=True)


# ------------------------------------------------------------------ render
def render() -> None:
    st.subheader("📋 Backlog & Coverage")
    st.caption(
        "Counts match Explorer: each row = case × country × device. "
        "Backlog = any status except Automated* and Automation not applicable."
    )

    with st.spinner("Computing backlog stats…"):
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
            "Cov. vs Total %": st.column_config.NumberColumn("Cov. vs Total",       format="%.1f%%"),
            "Cov. vs Auto. %": st.column_config.NumberColumn("Cov. vs Automatable", format="%.1f%%"),
            "N/A %":           st.column_config.NumberColumn("N/A %",               format="%.1f%%"),
        },
    )

    st.divider()

    st.markdown("#### Detail by Business Unit")
    bu_choice = st.selectbox(
        "Business Unit", WEBSITE_BUS, key="bl_bu_detail",
        label_visibility="collapsed",
    )
    _detail_view(bu_choice)
