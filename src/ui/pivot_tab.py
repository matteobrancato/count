"""Tab 1 — BU explorer: pivot builder (top) + test list (bottom).

UX:
    - Dropdown to pick a BU (alphabetical + "Next Gen" and "Mobile App" below a separator)
    - KPI row showing total, automated, coverage %
    - "Pivot builder" — user picks Rows / Columns / (implicit value = count of case_id)
      and gets an Excel-style crosstab on filtered cases
    - Multi-filter bar (type, deprecated, priority, framework, device, country, section)
    - Result list at the bottom with clickable links
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ..bu_rules import ALL_RULES, WEBSITE_BUS
from ..rules_engine import evaluate_rules

# ---------------- BU dropdown order
BU_ORDER = [
    "Drogas", "ICI Paris XL", "Kruidvat", "Marionnaud", "Savers",
    "Superdrug", "The Perfume Shop", "Trekpleister", "Watsons",
]
SPECIAL_BUS = ["Next Gen", "Mobile App (combined)"]


def _rules_for_choice(choice: str):
    if choice == "Next Gen":
        return [r for r in ALL_RULES if r.scope == "next_gen"]
    if choice == "Mobile App (combined)":
        return [r for r in ALL_RULES if r.scope == "mobile_app"]
    return [r for r in ALL_RULES if r.bu == choice and r.scope == "website"]


# ------------------------------------------------------------------ ui helpers
def _kpi_row(raw: pd.DataFrame, automated: pd.DataFrame) -> None:
    cols = st.columns(4)
    non_dep = raw[raw["deprecated"] == False] if not raw.empty else raw  # noqa: E712
    total = int(len(non_dep))
    auto_ids = set(automated["case_id"].unique()) if not automated.empty else set()
    auto_n = int(non_dep["case_id"].isin(auto_ids).sum()) if not non_dep.empty else 0
    pct = (auto_n / total * 100) if total else 0
    cols[0].metric("Total cases (non-deprecated)", f"{total:,}")
    cols[1].metric("Automated (unique)", f"{auto_n:,}")
    cols[2].metric("Automation coverage", f"{pct:.1f}%")
    cols[3].metric("Expanded rows", f"{len(automated):,}",
                   help="Each row = one (case × country × device) expansion used in Tab 2.")


def _filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    with st.expander("🔎 Filters", expanded=False):
        c1, c2, c3 = st.columns(3)
        types = c1.multiselect("Type", sorted(df["type_label"].dropna().unique()))
        prios = c2.multiselect("Priority", sorted(df["priority_label"].dropna().unique()))
        dep = c3.selectbox("Deprecated", ["(all)", "No", "Yes"], index=1)
        c4, c5 = st.columns(2)
        devices = c4.multiselect("Device", sorted(df["device"].dropna().unique()))
        sections = c5.multiselect("Section (top-level)", sorted({
            (p.split(">")[0].strip() if p else "(root)") for p in df["section_path"].fillna("")
        }))
    out = df
    if types:
        out = out[out["type_label"].isin(types)]
    if prios:
        out = out[out["priority_label"].isin(prios)]
    if dep != "(all)":
        out = out[out["deprecated"] == (dep == "Yes")]
    if devices:
        out = out[out["device"].isin(devices)]
    if sections:
        top = out["section_path"].fillna("").map(lambda p: p.split(">")[0].strip() if p else "(root)")
        out = out[top.isin(sections)]
    return out


def _pivot_builder(df: pd.DataFrame) -> None:
    st.markdown("#### Pivot view")
    if df.empty:
        st.info("No cases match the current filters.")
        return
    cols_avail = [
        "device", "priority_label", "type_label", "deprecated",
        "automation_tool", "prod_sanity",
    ]
    cols_avail = [c for c in cols_avail if c in df.columns]
    c1, c2 = st.columns(2)
    rows = c1.multiselect("Rows", cols_avail, default=["device"])
    cols = c2.multiselect("Columns", [c for c in cols_avail if c not in rows])
    if not rows and not cols:
        st.caption("Pick at least one row or column.")
        return
    try:
        pv = pd.pivot_table(
            df, values="case_id", index=rows or None, columns=cols or None,
            aggfunc="count", fill_value=0, margins=True, margins_name="Total",
        )
        st.dataframe(pv, use_container_width=True)
    except Exception as exc:  # pragma: no cover — defensive
        st.error(f"Pivot failed: {exc}")


def _list_view(df: pd.DataFrame) -> None:
    st.markdown("#### Test cases")
    if df.empty:
        st.info("No cases to show.")
        return
    display = df[[
        "case_id", "title", "type_label", "priority_label", "device",
        "section_path", "automation_tool", "prod_sanity", "deprecated", "url",
    ]].rename(columns={
        "case_id": "ID", "title": "Title", "type_label": "Type",
        "priority_label": "Priority", "device": "Device",
        "section_path": "Section", "automation_tool": "Tool",
        "prod_sanity": "Prod Sanity", "deprecated": "Deprecated", "url": "URL",
    })
    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "URL": st.column_config.LinkColumn("Link", display_text="Open ↗"),
            "ID": st.column_config.NumberColumn(width="small"),
            "Title": st.column_config.TextColumn(width="large"),
        },
    )
    st.caption(f"{len(display):,} rows")


# --------------------------------------------------------------------- render
def render() -> None:
    st.subheader("📊 BU Explorer")
    options = BU_ORDER + ["── Other ──"] + SPECIAL_BUS
    choice = st.selectbox("Business Unit", options, index=0, key="tab1_bu")
    if choice == "── Other ──":
        st.stop()

    rules = _rules_for_choice(choice)
    if not rules:
        st.warning("No rules defined for this BU yet.")
        return

    result = evaluate_rules(tuple(r.name for r in rules))
    raw = result.raw_cases
    automated = result.automated

    _kpi_row(raw, automated)
    st.divider()
    filtered = _filters(raw)
    _pivot_builder(filtered)
    st.divider()
    _list_view(filtered)
