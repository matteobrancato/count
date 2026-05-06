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
    "testim_desktop": "Testim.io | Desktop",
    "testim_mobile":  "Testim.io | Mobile",
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
    "device":          "Device",
    "priority_label":  "Priority",
    "framework":       "Framework",
    "country_label":   "Country",
    "automation_tool": "Automation Tool",
    "is_prod_sanity":  "Prod Sanity",
    "rule_name":       "Rule",
    "status_value":    "Status",
    "case_id":         "ID",
    "title":           "Title",
    "section_path":    "Section",
    "url":             "URL",
}


# ------------------------------------------------------------------ helpers
def _rules_for_choice(choice: str):
    if choice == "Microservices":
        return [r for r in ALL_RULES if r.scope == "next_gen"]
    if choice == "Mobile Application":
        return [r for r in ALL_RULES if r.scope == "mobile_app"]
    return [r for r in ALL_RULES if r.bu == choice and r.scope == "website"]


def _dedup_auto(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.drop_duplicates(subset=["case_id", "country_label", "device"])


def _apply_display_values(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "framework" in df.columns:
        df["framework"] = df["framework"].map(FRAMEWORK_LABELS).fillna(df["framework"])
    if "country_label" in df.columns:
        df["country_label"] = df["country_label"].map(lambda c: COUNTRY_NAMES.get(c, c))
    return df


# ------------------------------------------------------------------ calculation expander
def _calc_expander(rules: list, auto_df: pd.DataFrame) -> None:
    """Collapsible 'how are these numbers calculated?' section."""
    with st.expander("ℹ️ How are these numbers calculated?", expanded=False):

        st.markdown(
            "A test case is counted as **Automated** when **all** of the following are true:\n"
            "1. Its automation status field contains one of the *automated values* for its rule\n"
            "2. Its country token appears in the rule's country filter\n"
            "3. It is **not deprecated**\n\n"
            "**Expansion:** a case covering N countries generates N rows (one per country). "
            "TestIM Desktop and TestIM Mobile are separate rules, so a case that is automated "
            "for both adds one Desktop row **and** one Mobile row.\n\n"
            "**Deduplication:** each *(case_id, country, device)* combination is counted only once "
            "— if two rules match the same case/country/device the row is not double-counted."
        )

        # Per-rule row counts from the auto DataFrame
        if not auto_df.empty and "rule_name" in auto_df.columns:
            st.markdown("**Rows contributed by each rule (before deduplication):**")
            rule_counts = (
                auto_df.groupby(["rule_name", "framework", "country_label", "device"])
                .size()
                .reset_index(name="Rows")
                .sort_values(["rule_name", "country_label", "device"])
            )
            rule_counts["Framework"] = rule_counts["framework"].map(FRAMEWORK_LABELS).fillna(rule_counts["framework"])
            rule_counts["Country"]   = rule_counts["country_label"].map(
                lambda c: COUNTRY_NAMES.get(c, c)
            )
            st.dataframe(
                rule_counts[["rule_name", "Framework", "Country", "device", "Rows"]]
                .rename(columns={"rule_name": "Rule", "device": "Device"}),
                use_container_width=True, hide_index=True,
            )
            total_before = int(rule_counts["Rows"].sum())
            total_after  = len(auto_df)
            if total_before != total_after:
                st.caption(
                    f"Before dedup: {total_before:,} rows → "
                    f"after dedup on (case_id, country, device): **{total_after:,} rows**"
                )

        st.markdown("**Rules configured for this BU:**")
        fw_label = {"java": "Java", "testim_desktop": "TestIM Desktop",
                    "testim_mobile": "TestIM Mobile", "mobile_app": "Mobile App"}
        tbl = []
        for r in rules:
            countries = ", ".join(r.country_labels.get(t, t) for t in r.countries_filter) or "all"
            tbl.append({
                "Rule":            r.name,
                "Framework":       fw_label.get(r.framework, r.framework),
                "Status field":    r.status_field_label,
                "Countries":       countries,
                "Automated values": ", ".join(r.automated_values),
            })
        st.dataframe(pd.DataFrame(tbl), use_container_width=True, hide_index=True)


# ------------------------------------------------------------------ KPI row
def _kpi_row(raw: pd.DataFrame, auto_dedup: pd.DataFrame) -> None:
    non_dep = raw[~raw["deprecated"]] if not raw.empty else raw
    total   = len(non_dep)
    desktop = int((auto_dedup["device"] == "Desktop").sum()) if not auto_dedup.empty else 0
    mobile  = int((auto_dedup["device"] == "Mobile").sum())  if not auto_dedup.empty else 0

    c1, c2, c3 = st.columns(3)
    c1.metric(
        "Total cases", f"{total:,}",
        help="All non-deprecated cases in this BU's suite(s).",
    )
    c2.metric(
        "Desktop", f"{desktop:,}",
        help="Expanded Desktop rows (case × country).",
    )
    c3.metric(
        "Mobile", f"{mobile:,}",
        help="Expanded Mobile rows (case × country).",
    )


# ------------------------------------------------------------------ filters
def _auto_filters(df: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    """Filter the automated (expanded) DataFrame."""
    if df.empty:
        return df

    with st.expander("🔎 Filters", expanded=False):
        # ── Row 1: Country · Device · Framework · Priority ──────────────────
        r1 = st.columns(4)

        # Country (only shown when multiple countries exist)
        sel_ctry: list[str] = []
        if "country_label" in df.columns:
            countries_iso   = sorted(df["country_label"].dropna().unique())
            country_display = [COUNTRY_NAMES.get(c, c) for c in countries_iso]
            display_to_iso  = {COUNTRY_NAMES.get(c, c): c for c in countries_iso}
            if len(countries_iso) > 1:
                sel_ctry_disp = r1[0].multiselect(
                    "Country", country_display, key=f"{key_prefix}_ctry")
                sel_ctry = [display_to_iso[d] for d in sel_ctry_disp]

        # Device (uses device_original so "Both" is selectable)
        dev_col      = "device_original" if "device_original" in df.columns else "device"
        devices_orig = sorted(
            df[dev_col].dropna().unique(),
            key=lambda d: {"Desktop": 0, "Mobile": 1, "Both": 2}.get(d, 3),
        )
        sel_dev_orig = r1[1].multiselect("Device", devices_orig, key=f"{key_prefix}_dev")

        # Framework (internal codes → readable labels)
        frameworks = sorted(df["framework"].dropna().unique())
        fw_labels  = [FRAMEWORK_LABELS.get(f, f) for f in frameworks]
        sel_fw_lbl = r1[2].multiselect("Framework", fw_labels, key=f"{key_prefix}_fw")
        sel_fw     = [frameworks[i] for i, lbl in enumerate(fw_labels) if lbl in sel_fw_lbl]

        # Priority
        priorities = sorted(df["priority_label"].dropna().unique())
        sel_prio   = r1[3].multiselect("Priority", priorities, key=f"{key_prefix}_prio")

        # ── Row 2: Section · Prod Sanity · Smoke ────────────────────────────
        r2 = st.columns([2, 1, 1])

        sections = sorted({
            p.split(">")[0].strip() if p else "(root)"
            for p in df["section_path"].fillna("")
        })
        sel_sect   = r2[0].multiselect("Section", sections, key=f"{key_prefix}_sect")
        prod_only  = r2[1].checkbox("Production Sanity",    key=f"{key_prefix}_prod")
        smoke_only = r2[2].checkbox("Smoke (Highest Priority)", key=f"{key_prefix}_smoke")

    # ── Apply filters ────────────────────────────────────────────────────────
    out = df
    if sel_ctry:
        out = out[out["country_label"].isin(sel_ctry)]
    if sel_dev_orig:
        dev_col = "device_original" if "device_original" in out.columns else "device"
        out = out[out[dev_col].isin(sel_dev_orig)]
    if sel_fw:
        out = out[out["framework"].isin(sel_fw)]
    if sel_prio:
        out = out[out["priority_label"].isin(sel_prio)]
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
def _pivot_builder(
    df: pd.DataFrame,
    key_prefix: str,
    default_rows: list[str] | None = None,
    default_cols: list[str] | None = None,
) -> None:
    """Excel-style pivot: row/col selectors + crosstab.

    *default_rows* / *default_cols* set the initial selection (display labels).
    Including the BU name in *key_prefix* ensures defaults reset when switching BUs.
    """
    st.markdown("#### 📊 Pivot")
    if df.empty:
        st.info("No automated cases match the current filters.")
        return

    # Internal columns available for pivoting (rule_name and is_prod_sanity excluded —
    # the latter is already exposed as a dedicated filter checkbox)
    internal_cols = [c for c in [
        "device", "priority_label", "framework", "country_label",
        "automation_tool", "status_value",
    ] if c in df.columns]

    # Display labels for the selectors
    display_cols = [COL_LABELS.get(c, c) for c in internal_cols]
    lbl_to_internal = {COL_LABELS.get(c, c): c for c in internal_cols}

    # Validate defaults against available columns (avoids Streamlit errors)
    safe_rows = [l for l in (default_rows or ["Device"]) if l in display_cols]
    safe_cols = [l for l in (default_cols or [])          if l in display_cols]

    c1, c2 = st.columns(2)
    row_sel_lbl = c1.multiselect("Rows", display_cols, default=safe_rows,
                                 key=f"{key_prefix}_pv_rows")
    col_sel_lbl = c2.multiselect(
        "Columns",
        [l for l in display_cols if l not in row_sel_lbl],
        default=[l for l in safe_cols if l not in row_sel_lbl],
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
    name = col[len("status_"):] if col.startswith("status_") else col
    prefix = "Automation Status "
    if name.startswith(prefix):
        name = name[len(prefix):]
    if not name:
        name = "Status"
    name = name.replace("Testim Desktop", "Testim.io | Desktop")
    name = name.replace("Testim Mobile View", "Testim.io | Mobile")
    return name


# ------------------------------------------------------------------ test list
def _list_view(auto_df: pd.DataFrame, raw_df: pd.DataFrame) -> None:
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
        st.caption(f"{n_rows:,} rows")


# ------------------------------------------------------------------ render
def render() -> None:
    st.subheader("📊 Business Units")

    options = BU_ORDER + ["─", "Microservices", "Mobile Application"]
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

    # ---- Filters + Pivot (on automated expanded df)
    # key_prefix includes choice so switching BU resets widget state to correct defaults
    bu_key = choice.replace(" ", "_")
    if choice == "Microservices":
        pv_rows, pv_cols = ["Framework"], ["Country"]
    elif choice == "Mobile Application":
        pv_rows, pv_cols = ["Country"], ["Automation Tool"]
    else:
        pv_rows, pv_cols = ["Device"], ["Framework", "Country"]

    _calc_expander(rules, auto_all)
    filtered_auto = _auto_filters(auto_all, key_prefix=f"t1_{bu_key}")
    _pivot_builder(filtered_auto, key_prefix=f"t1_{bu_key}",
                   default_rows=pv_rows, default_cols=pv_cols)
    st.divider()

    # ---- Test list
    _list_view(filtered_auto, raw)
