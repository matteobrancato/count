"""Tab 1 — BU Explorer.

Layout:
  ┌─────────────────────────────────────────────────────┐
  │  [BU selector]  [framework chips]                   │
  │  KPI row: Total (non-dep) | Automated | Coverage %  │
  ├─────────────────────────────────────────────────────┤
  │  Filters (priority, device, country, section, ...)  │
  │  PIVOT on automated cases (like Excel screenshot)   │
  ├─────────────────────────────────────────────────────┤
  │  Test list — unique automated cases + URL links     │
  └─────────────────────────────────────────────────────┘

The pivot works on the EXPANDED automated DataFrame (one row per case × device,
after Both→Desktop+Mobile expansion). This matches the Excel pivots in the
screenshots exactly: Device rows, Count of ID, Grand Total.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ..bu_rules import ALL_RULES
from ..rules_engine import evaluate_rules, ExpansionResult

# ------------------------------------------------------------------ constants
BU_ORDER = [
    "Drogas", "ICI Paris XL", "Kruidvat", "Marionnaud", "Savers",
    "Superdrug", "The Perfume Shop", "Trekpleister", "Watsons",
]

FRAMEWORK_LABELS = {
    "java":           "Legacy Java",
    "testim_desktop": "TestIM Desktop",
    "testim_mobile":  "TestIM Mobile",
    "mobile_app":     "Mobile App",
}


# ------------------------------------------------------------------ helpers
def _rules_for_choice(choice: str):
    if choice == "Next Gen":
        return [r for r in ALL_RULES if r.scope == "next_gen"]
    if choice == "Mobile App (combined)":
        return [r for r in ALL_RULES if r.scope == "mobile_app"]
    return [r for r in ALL_RULES if r.bu == choice and r.scope == "website"]


def _dedup_auto(df: pd.DataFrame) -> pd.DataFrame:
    """Remove duplicate (case_id, device) rows from multi-rule overlap."""
    if df.empty:
        return df
    return df.drop_duplicates(subset=["case_id", "country_label", "device"])


# ------------------------------------------------------------------ KPI row
def _kpi_row(raw: pd.DataFrame, auto_dedup: pd.DataFrame) -> None:
    non_dep = raw[~raw["deprecated"]] if not raw.empty else raw
    total   = len(non_dep)
    auto_n  = auto_dedup["case_id"].nunique() if not auto_dedup.empty else 0
    pct     = (auto_n / total * 100) if total else 0.0

    desktop = int((auto_dedup["device"] == "Desktop").sum()) if not auto_dedup.empty else 0
    mobile  = int((auto_dedup["device"] == "Mobile").sum())  if not auto_dedup.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(
        "Total cases (non-dep)", f"{total:,}",
        help="Tutti i casi non-deprecated nel/nei suite di questa BU.",
    )
    c2.metric(
        "Automated cases", f"{auto_n:,}",
        help=(
            "Casi unici (case_id distinti) che matchano almeno una regola di automazione. "
            "Un caso che copre più countries conta 1 solo. "
            "Il pivot mostra invece i conteggi espansi per country × device."
        ),
    )
    c3.metric(
        "Coverage", f"{pct:.1f}%",
        help="Automated cases / Total cases (non-dep).",
    )
    c4.metric(
        "Desktop rows", f"{desktop:,}",
        help=(
            "Righe device=Desktop nel DataFrame espanso (country × device). "
            "Un caso con device=Both in 3 countries vale 3 qui."
        ),
    )
    c5.metric(
        "Mobile rows", f"{mobile:,}",
        help=(
            "Righe device=Mobile nel DataFrame espanso (country × device). "
            "Un caso con device=Both in 3 countries vale 3 qui."
        ),
    )


# ------------------------------------------------------------------ filters
def _auto_filters(df: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    """Filter the automated (expanded) DataFrame."""
    if df.empty:
        return df

    with st.expander("🔎 Filters", expanded=False):
        c1, c2, c3 = st.columns(3)

        priorities = sorted(df["priority_label"].dropna().unique())
        sel_prio = c1.multiselect("Priority", priorities, key=f"{key_prefix}_prio")

        devices = sorted(df["device"].dropna().unique())
        sel_dev = c2.multiselect("Device", devices, key=f"{key_prefix}_dev")

        frameworks = sorted(df["framework"].dropna().unique())
        fw_labels  = [FRAMEWORK_LABELS.get(f, f) for f in frameworks]
        sel_fw_lbl = c3.multiselect("Framework", fw_labels, key=f"{key_prefix}_fw")
        sel_fw     = [frameworks[i] for i, l in enumerate(fw_labels) if l in sel_fw_lbl]

        c4, c5 = st.columns(2)

        if "country_label" in df.columns:
            countries = sorted(df["country_label"].dropna().unique())
            if len(countries) > 1:
                sel_ctry = c4.multiselect("Country", countries, key=f"{key_prefix}_ctry")
            else:
                sel_ctry = []
        else:
            sel_ctry = []

        sections = sorted({
            p.split(">")[0].strip() if p else "(root)"
            for p in df["section_path"].fillna("")
        })
        sel_sect = c5.multiselect("Section (top-level)", sections, key=f"{key_prefix}_sect")

        prod_only = st.checkbox("Prod Sanity only", key=f"{key_prefix}_prod")
        smoke_only = st.checkbox("Smoke (Highest) only", key=f"{key_prefix}_smoke")

    out = df
    if sel_prio:
        out = out[out["priority_label"].isin(sel_prio)]
    if sel_dev:
        out = out[out["device"].isin(sel_dev)]
    if sel_fw:
        out = out[out["framework"].isin(sel_fw)]
    if sel_ctry:
        out = out[out["country_label"].isin(sel_ctry)]
    if sel_sect:
        top = out["section_path"].fillna("").map(
            lambda p: p.split(">")[0].strip() if p else "(root)"
        )
        out = out[top.isin(sel_sect)]
    if prod_only:
        out = out[out["is_prod_sanity"] == True]   # noqa: E712
    if smoke_only:
        out = out[out["priority_label"].str.lower().str.contains("highest", na=False)]

    return out


# ------------------------------------------------------------------ pivot
def _pivot_builder(df: pd.DataFrame, key_prefix: str) -> None:
    """Excel-style pivot: row/col selectors + crosstab."""
    st.markdown("#### 📊 Pivot")
    if df.empty:
        st.info("No automated cases match the current filters.")
        return

    pivot_cols = [c for c in [
        "device", "priority_label", "framework", "country_label",
        "is_prod_sanity", "rule_name", "status_value",
    ] if c in df.columns]

    c1, c2 = st.columns(2)
    row_sel = c1.multiselect("Rows", pivot_cols, default=["device"],
                             key=f"{key_prefix}_pv_rows")
    col_sel = c2.multiselect("Columns", [c for c in pivot_cols if c not in row_sel],
                             key=f"{key_prefix}_pv_cols")

    if not row_sel and not col_sel:
        st.caption("Select at least one row or column field.")
        return

    try:
        pv = pd.pivot_table(
            df,
            values="case_id",
            index=row_sel   or None,
            columns=col_sel or None,
            aggfunc="count",
            fill_value=0,
            margins=True,
            margins_name="Grand Total",
        )
        st.dataframe(pv, use_container_width=True)
    except Exception as exc:
        st.error(f"Pivot error: {exc}")


# ------------------------------------------------------------------ test list
def _list_view(auto_df: pd.DataFrame, raw_df: pd.DataFrame) -> None:
    """Show automated test cases: one row per (case_id × country_label).

    Source is `auto_df` (the expanded + filtered DataFrame) so the table is
    consistent with the pivot above.  Device expansion is collapsed: a case
    with device=Both appears once per country (not twice for Desktop+Mobile).
    """
    st.markdown("#### 🗂 Test list (automated cases)")
    if auto_df.empty:
        st.info("No automated cases.")
        return

    # One row per (case_id, country_label) — collapse device expansion
    detail = auto_df.drop_duplicates(subset=["case_id", "country_label"]).copy()

    # Build display columns from auto_df (which already has title, url, section_path)
    show = []
    col_renames = {}
    for col, lbl in [
        ("case_id",       "ID"),
        ("title",         "Title"),
        ("priority_label","Priority"),
        ("country_label", "Country"),
        ("framework",     "Framework"),
        ("section_path",  "Section"),
        ("is_prod_sanity","Prod Sanity"),
        ("url",           "URL"),
    ]:
        if col in detail.columns:
            show.append(col)
            col_renames[col] = lbl

    disp = detail[show].rename(columns=col_renames) if show else detail

    # Map framework codes to readable labels
    if "Framework" in disp.columns:
        disp = disp.copy()
        disp["Framework"] = disp["Framework"].map(FRAMEWORK_LABELS).fillna(disp["Framework"])

    st.dataframe(
        disp,
        use_container_width=True,
        hide_index=True,
        column_config={
            "URL":        st.column_config.LinkColumn("Link", display_text="Open ↗"),
            "ID":         st.column_config.NumberColumn(width="small"),
            "Title":      st.column_config.TextColumn(width="large"),
            "Prod Sanity": st.column_config.CheckboxColumn(width="small"),
        },
    )
    n_cases = detail["case_id"].nunique()
    n_rows  = len(detail)
    if n_rows == n_cases:
        st.caption(f"{n_cases:,} automated test cases")
    else:
        st.caption(
            f"{n_rows:,} righe — {n_cases:,} casi unici × country "
            f"(un caso che copre N paesi appare N volte)"
        )


# ------------------------------------------------------------------ render
def render() -> None:
    st.subheader("📊 BU Explorer")

    options = BU_ORDER + ["─────────────", "Next Gen", "Mobile App (combined)"]
    choice  = st.selectbox("Business Unit", options, index=0, key="tab1_bu")
    if choice.startswith("─"):
        st.stop()

    rules = _rules_for_choice(choice)
    if not rules:
        st.warning("No rules defined for this BU.")
        return

    with st.spinner(f"Loading {choice}…"):
        result: ExpansionResult = evaluate_rules(tuple(r.name for r in rules))

    raw      = result.raw_cases
    auto_all = _dedup_auto(result.automated)

    if auto_all.empty and raw.empty:
        st.warning("No cases loaded. Check the Debug tab for field mapping issues.")
        return

    # ---- KPI
    _kpi_row(raw, auto_all)
    st.divider()

    # ---- Filters + Pivot (on automated expanded df)
    filtered_auto = _auto_filters(auto_all, key_prefix="t1")
    _pivot_builder(filtered_auto, key_prefix="t1")
    st.divider()

    # ---- Test list
    _list_view(filtered_auto, raw)
