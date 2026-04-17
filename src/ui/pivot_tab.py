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
    "java":           "Java Testing Framework",
    "testim_desktop": "TestIM - Desktop View",
    "testim_mobile":  "TestIM - Mobile View",
    "mobile_app":     "Mobile Application",
}

# ISO country code → full name
COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria",
    "BE": "Belgium",
    "CH": "Switzerland",
    "CZ": "Czech Republic",
    "FR": "France",
    "GB": "United Kingdom",
    "HU": "Hungary",
    "IE": "Ireland",
    "IT": "Italy",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "NL": "Netherlands",
    "RO": "Romania",
    "SK": "Slovakia",
    "TR": "Turkey",
    "UK": "United Kingdom",
}

# Human-readable labels for every internal column name that surfaces in the UI
COL_LABELS: dict[str, str] = {
    "device":         "Device",
    "priority_label": "Priority",
    "framework":      "Framework",
    "country_label":  "Country",
    "is_prod_sanity": "Prod Sanity",
    "rule_name":      "Rule",
    "status_value":   "Status",
    "case_id":        "ID",
    "title":          "Title",
    "section_path":   "Section",
    "url":            "URL",
}


# ------------------------------------------------------------------ helpers
def _rules_for_choice(choice: str):
    if choice == "Microservices":
        return [r for r in ALL_RULES if r.scope == "next_gen"]
    if choice == "Mobile Appplication":
        return [r for r in ALL_RULES if r.scope == "mobile_app"]
    return [r for r in ALL_RULES if r.bu == choice and r.scope == "website"]


def _dedup_auto(df: pd.DataFrame) -> pd.DataFrame:
    """Remove duplicate (case_id, device) rows from multi-rule overlap."""
    if df.empty:
        return df
    return df.drop_duplicates(subset=["case_id", "country_label", "device"])


def _apply_display_values(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with human-readable values in known columns."""
    df = df.copy()
    if "framework" in df.columns:
        df["framework"] = df["framework"].map(FRAMEWORK_LABELS).fillna(df["framework"])
    return df


# ------------------------------------------------------------------ KPI row
def _kpi_row(raw: pd.DataFrame, auto_dedup: pd.DataFrame) -> None:
    non_dep = raw[~raw["deprecated"]] if not raw.empty else raw
    total   = len(non_dep)
    auto_n  = auto_dedup["case_id"].nunique() if not auto_dedup.empty else 0
    pct     = (auto_n / total * 100) if total else 0.0

    desktop = int((auto_dedup["device"] == "Desktop").sum()) if not auto_dedup.empty else 0
    mobile  = int((auto_dedup["device"] == "Mobile").sum())  if not auto_dedup.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(
        "Total cases", f"{total:,}",
        help="All non-deprecated cases in this BU's suite(s).",
    )
    c2.metric(
        "Coverage", f"{pct:.1f}%",
        help="Automated cases / Total cases (non-deprecated).",
    )
    c3.metric(
        "Desktop", f"{desktop:,}",
        help=(
            "Expanded Desktop rows (country × device). "
            "A case with Device=Both across 3 countries contributes 3 here."
        ),
    )
    c4.metric(
        "Mobile", f"{mobile:,}",
        help=(
            "Expanded Mobile rows (country × device). "
            "A case with Device=Both across 3 countries contributes 3 here."
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

        # Device filter uses device_original so "Both" is selectable
        dev_col = "device_original" if "device_original" in df.columns else "device"
        devices_orig = sorted(df[dev_col].dropna().unique(),
                              key=lambda d: {"Desktop": 0, "Mobile": 1, "Both": 2}.get(d, 3))
        sel_dev_orig = c2.multiselect("Device", devices_orig, key=f"{key_prefix}_dev")

        frameworks = sorted(df["framework"].dropna().unique())
        fw_labels  = [FRAMEWORK_LABELS.get(f, f) for f in frameworks]
        sel_fw_lbl = c3.multiselect("Framework", fw_labels, key=f"{key_prefix}_fw")
        sel_fw     = [frameworks[i] for i, lbl in enumerate(fw_labels) if lbl in sel_fw_lbl]

        c4, c5 = st.columns(2)

        if "country_label" in df.columns:
            countries_iso = sorted(df["country_label"].dropna().unique())
            # Show full country names; map back to ISO for filtering
            country_display = [COUNTRY_NAMES.get(c, c) for c in countries_iso]
            display_to_iso  = {COUNTRY_NAMES.get(c, c): c for c in countries_iso}
            if len(countries_iso) > 1:
                sel_ctry_disp = c4.multiselect("Country", country_display, key=f"{key_prefix}_ctry")
                sel_ctry = [display_to_iso[d] for d in sel_ctry_disp]
            else:
                sel_ctry = []
        else:
            sel_ctry = []

        sections = sorted({
            p.split(">")[0].strip() if p else "(root)"
            for p in df["section_path"].fillna("")
        })
        sel_sect = c5.multiselect("Section (top-level)", sections, key=f"{key_prefix}_sect")

        prod_only  = st.checkbox("Prod Sanity only", key=f"{key_prefix}_prod")
        smoke_only = st.checkbox("Smoke (Highest) only", key=f"{key_prefix}_smoke")

    out = df
    if sel_prio:
        out = out[out["priority_label"].isin(sel_prio)]
    if sel_dev_orig:
        dev_col = "device_original" if "device_original" in out.columns else "device"
        out = out[out[dev_col].isin(sel_dev_orig)]
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

    # Internal columns available for pivoting (rule_name excluded — internal construct)
    internal_cols = [c for c in [
        "device", "priority_label", "framework", "country_label",
        "is_prod_sanity", "status_value",
    ] if c in df.columns]

    # Display labels for the selectors
    display_cols = [COL_LABELS.get(c, c) for c in internal_cols]
    lbl_to_internal = {COL_LABELS.get(c, c): c for c in internal_cols}

    c1, c2 = st.columns(2)
    row_sel_lbl = c1.multiselect("Rows", display_cols, default=["Device"],
                                 key=f"{key_prefix}_pv_rows")
    col_sel_lbl = c2.multiselect(
        "Columns",
        [l for l in display_cols if l not in row_sel_lbl],
        key=f"{key_prefix}_pv_cols",
    )

    if not row_sel_lbl and not col_sel_lbl:
        st.caption("Select at least one row or column field.")
        return

    row_sel = [lbl_to_internal[l] for l in row_sel_lbl]
    col_sel = [lbl_to_internal[l] for l in col_sel_lbl]

    # Build a display copy with readable values (framework codes → labels)
    disp_df = _apply_display_values(df)
    # Rename columns so pivot headers are clean
    disp_df = disp_df.rename(columns=COL_LABELS)

    row_disp = row_sel_lbl or None
    col_disp = col_sel_lbl or None

    try:
        pv = pd.pivot_table(
            disp_df,
            values="ID",
            index=row_disp,
            columns=col_disp,
            aggfunc="count",
            fill_value=0,
            margins=True,
            margins_name="Total",
        )
        st.dataframe(pv, use_container_width=True)
    except Exception as exc:
        st.error(f"Pivot error: {exc}")


# ------------------------------------------------------------------ test list helpers
def _status_col_label(col: str) -> str:
    """Convert internal status column name to a short display label.

    "status_Automation Status MFR"          → "MFR"
    "status_Automation Status MRN SPR"      → "MRN SPR"
    "status_Automation Status Testim Desktop" → "TestIM Desktop"
    "status_Automation Status Testim Mobile View" → "TestIM Mobile"
    "status_Automation Status"              → "Status"
    """
    name = col[len("status_"):] if col.startswith("status_") else col
    prefix = "Automation Status "
    if name.startswith(prefix):
        name = name[len(prefix):]
    if not name:
        name = "Status"
    name = name.replace("Testim Desktop", "TestIM Desktop")
    name = name.replace("Testim Mobile View", "TestIM Mobile")
    return name


# ------------------------------------------------------------------ test list
def _list_view(auto_df: pd.DataFrame, raw_df: pd.DataFrame) -> None:
    """Show automated test cases: one row per (case_id × country_label)."""
    st.markdown("#### 🗂 Test list")
    if auto_df.empty:
        st.info("No automated cases.")
        return

    # One row per (case_id, country_label) — collapse device expansion
    detail = auto_df.drop_duplicates(subset=["case_id", "country_label"]).copy()

    # ---- Merge automation-status columns from raw_df ----
    # raw_df has columns like "status_Automation Status MFR", etc.
    # We only include columns that have at least one non-null value for the
    # cases visible in this view — so the table adapts automatically per BU.
    status_raw_cols: list[str] = []
    if not raw_df.empty:
        all_status = [c for c in raw_df.columns if c.startswith("status_")]
        if all_status:
            case_ids = set(detail["case_id"].unique())
            raw_sub  = raw_df[raw_df["case_id"].isin(case_ids)].drop_duplicates("case_id")
            active   = [c for c in all_status if raw_sub[c].notna().any()]
            if active:
                detail = detail.merge(
                    raw_sub[["case_id"] + active], on="case_id", how="left"
                )
                status_raw_cols = active

    # ---- Build display table ----
    base_cols = []
    col_renames: dict[str, str] = {}
    for col, lbl in [
        ("case_id",        "ID"),
        ("title",          "Title"),
        ("priority_label", "Priority"),
        ("country_label",  "Country"),
        ("framework",      "Framework"),
        ("section_path",   "Section"),
        ("is_prod_sanity", "Prod Sanity"),
    ]:
        if col in detail.columns:
            base_cols.append(col)
            col_renames[col] = lbl

    # Status columns — renamed to short labels
    status_display: dict[str, str] = {c: _status_col_label(c) for c in status_raw_cols}
    col_renames.update(status_display)

    # URL last
    if "url" in detail.columns:
        base_cols.append("url")
        col_renames["url"] = "URL"

    show = [c for c in base_cols + status_raw_cols if c in detail.columns]
    disp = detail[show].rename(columns=col_renames)

    if "Framework" in disp.columns:
        disp = disp.copy()
        disp["Framework"] = disp["Framework"].map(FRAMEWORK_LABELS).fillna(disp["Framework"])

    # Country full names
    if "Country" in disp.columns:
        disp = disp.copy()
        disp["Country"] = disp["Country"].map(lambda c: COUNTRY_NAMES.get(c, c))

    col_cfg: dict = {
        "URL":         st.column_config.LinkColumn("Link", display_text="Open ↗"),
        "ID":          st.column_config.NumberColumn(width="small"),
        "Title":       st.column_config.TextColumn(width="large"),
        "Prod Sanity": st.column_config.CheckboxColumn(width="small"),
    }
    # Status columns as plain text (values are already string labels)
    for raw_col, disp_lbl in status_display.items():
        col_cfg[disp_lbl] = st.column_config.TextColumn(disp_lbl, width="small")

    st.dataframe(disp, use_container_width=True, hide_index=True, column_config=col_cfg)

    n_cases = detail["case_id"].nunique()
    n_rows  = len(detail)
    if n_rows == n_cases:
        st.caption(f"{n_cases:,} automated test cases")
    else:
        st.caption(f"{n_rows:,} rows — {n_cases:,} unique test cases × country")


# ------------------------------------------------------------------ render
def render() -> None:
    st.subheader("📊 Business Units")

    options = BU_ORDER + ["─────────────", "Microservices", "Mobile Appplication"]
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
