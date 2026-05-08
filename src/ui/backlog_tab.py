"""Backlog & Coverage tab — big_regr regression baseline.

Baseline definition
───────────────────
  A case enters the baseline if it has the label "big_regr_desktop" and/or
  "big_regr_mobile" in the TestRail Labels field (non-deprecated).

Device expansion
────────────────
  Comes from the labels, NOT from the Device field:
    big_regr_desktop only  → one Desktop row
    big_regr_mobile  only  → one Mobile row
    both labels            → one Desktop row + one Mobile row

Country expansion
─────────────────
  Website BUs  : multi_countries filtered to BU tokens (same as Explorer)
  Next Gen     : custom_country_coverage filtered to ALL_COUNTRY_TOKENS

Classification (per expanded row)
──────────────────────────────────
  automated      → (case_id, country_label, device) is in evaluate_rules().automated
  not_applicable → any status field = "Automation not applicable"
  backlog        → any other non-empty, non-automated status
  unknown        → no status field populated

Counts
──────
  Expanded  = one row per (case_id × country_label × device) — shown as main number
  Unique    = distinct case_id values within each category — shown in small text below

Scope: website + microservices (next_gen). Mobile App excluded for now.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from ..bu_rules import ALL_RULES, WEBSITE_BUS
from ..rules_engine import evaluate_rules

# ── constants ─────────────────────────────────────────────────────────────────
_LABEL_DESKTOP = "big_regr_desktop"
_LABEL_MOBILE  = "big_regr_mobile"

_STATUS_AUTO: set[str] = {
    "Automated", "Automated DEV", "Automated UAT", "Automated Prod",
}
_STATUS_NA: set[str] = {
    "Automation not applicable",
}

COUNTRY_NAMES: dict[str, str] = {
    "AT": "Austria",    "BE": "Belgium",     "CH": "Switzerland",
    "CZ": "Czech Rep.", "FR": "France",      "GB": "United Kingdom",
    "HU": "Hungary",    "IE": "Ireland",     "IT": "Italy",
    "LT": "Lithuania",  "LU": "Luxembourg",  "LV": "Latvia",
    "NL": "Netherlands","RO": "Romania",     "SK": "Slovakia",
    "TR": "Turkey",     "UK": "United Kingdom",
}


# ── load ──────────────────────────────────────────────────────────────────────
def _load_scope(scope: str) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    """Load ALL rules for a scope in ONE evaluate_rules call.

    Uses the same rule-name tuple as the Overview tab, so both tabs share
    a single @st.cache_data entry — no redundant processing.
    """
    rules  = [r for r in ALL_RULES if r.scope == scope]
    result = evaluate_rules(tuple(r.name for r in rules))
    raw    = result.raw_cases
    auto   = result.automated
    if not raw.empty:
        raw = raw[~raw["deprecated"]].reset_index(drop=True)
    return raw, auto, rules


def _filter_bu(
    raw: pd.DataFrame,
    auto: pd.DataFrame,
    rules: list,
    bu: str,
) -> tuple[pd.DataFrame, pd.DataFrame, list]:
    """Slice scope-wide DataFrames down to a single BU."""
    rules_bu   = [r for r in rules if r.bu == bu]
    suite_ids  = {r.suite_id for r in rules_bu}
    raw_bu     = raw[raw["suite_id"].isin(suite_ids)]  if not raw.empty  else raw
    auto_bu    = auto[auto["bu"] == bu]                if not auto.empty else auto
    return raw_bu, auto_bu, rules_bu


def _scoped_bus() -> list[tuple[str, str]]:
    """Return [(bu, scope), ...] for website BUs + next_gen BU."""
    pairs: list[tuple[str, str]] = [(bu, "website") for bu in WEBSITE_BUS]
    ng_bus = sorted({r.bu for r in ALL_RULES if r.scope == "next_gen"})
    for bu in ng_bus:
        pairs.append((bu, "next_gen"))
    return pairs


# ── expansion ─────────────────────────────────────────────────────────────────
def _pick_country_col(rules: list) -> str:
    """Return which raw_cases column to use for country expansion."""
    for r in rules:
        if getattr(r, "country_field_label", "multi_countries") == "custom_country_coverage":
            return "country_coverage"
    return "multi_countries"


def _expand_baseline(raw: pd.DataFrame, rules: list) -> pd.DataFrame:
    """Expand big_regr cases into (case_id × country_label × device) rows."""
    _empty = pd.DataFrame(columns=["case_id", "country_label", "device", "_cat_base"])

    if raw.empty:
        return _empty

    country_col = _pick_country_col(rules)
    if "labels" not in raw.columns or country_col not in raw.columns:
        return _empty

    # Build token → ISO label map for this BU
    token_label: dict[str, str] = {}
    for rule in rules:
        for tok in rule.countries_filter:
            token_label[tok] = rule.country_labels.get(tok, tok)
    all_tokens = set(token_label)

    # ── Initial status classification (combined — used as fallback) ──────────────
    # We compute a combined mask first so non-TestIM rules (Java, etc.) that use a
    # single status field still get classified correctly.  TestIM cases are then
    # re-classified per-device after expansion (see below).
    status_cols = [c for c in raw.columns if c.startswith("status_")]
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

    # ── Filter to baseline (big_regr labels) ──────────────────────────────────
    raw["_label_devs"] = raw["labels"].apply(
        lambda ls: (
            (["Desktop"] if _LABEL_DESKTOP in ls else []) +
            (["Mobile"]  if _LABEL_MOBILE  in ls else [])
        ) if isinstance(ls, list) else []
    )
    raw = raw[raw["_label_devs"].map(len) > 0]
    if raw.empty:
        return _empty

    # ── Country expansion ─────────────────────────────────────────────────────
    if all_tokens:
        raw["_countries"] = raw[country_col].apply(
            lambda mc: list({
                token_label[t]
                for t in (mc if isinstance(mc, list) else [])
                if t in all_tokens
            })
        )
        raw = raw[raw["_countries"].map(len) > 0]
    else:
        raw["_countries"] = raw.apply(lambda _: ["__ALL__"], axis=1)

    if raw.empty:
        return _empty

    raw = raw.explode("_countries").rename(columns={"_countries": "country_label"})

    # ── Device expansion from labels ──────────────────────────────────────────
    raw = raw.explode("_label_devs").rename(columns={"_label_devs": "device_exp"})

    # ── Device-specific re-classification (TestIM) ───────────────────────────
    # Problem 1: a case with Desktop="Ready" + Mobile="N/A" gets combined na_mask=True
    # → _cat_base="not_applicable" for BOTH device rows, wrongly excluding it from
    # the Desktop backlog.
    # Problem 2: a Java case with Automation Status="Not automated" but TestIM field
    # empty would be reset to "unknown" and lost from backlog.
    # Fix: re-classify a device row using its TestIM-specific status field ONLY when
    # that field is actually populated.  If empty (default / never set), keep the
    # initial classification based on combined status fields (which captures Java).
    _DEVICE_STATUS_COL = {
        "Desktop": "status_Automation Status Testim Desktop",
        "Mobile":  "status_Automation Status Testim Mobile View",
    }
    for dev, scol in _DEVICE_STATUS_COL.items():
        if scol not in raw.columns:
            continue
        dev_mask = raw["device_exp"] == dev
        if not dev_mask.any():
            continue
        col_vals = raw[scol]
        has_value = col_vals.notna() & (col_vals != "")
        # Only reclassify rows where the device-specific TestIM field has a value.
        raw.loc[dev_mask & has_value & col_vals.isin(_STATUS_NA), "_cat_base"] = "not_applicable"
        raw.loc[
            dev_mask
            & has_value
            & ~col_vals.isin(_STATUS_AUTO | _STATUS_NA),
            "_cat_base",
        ] = "backlog"

    # ── Dedup on (case_id, country_label, device) ─────────────────────────────
    return (
        raw[["case_id", "country_label", "device_exp", "_cat_base"]]
        .drop_duplicates(subset=["case_id", "country_label", "device_exp"])
        .rename(columns={"device_exp": "device"})
        .reset_index(drop=True)
    )


def _classify_expanded(expanded: pd.DataFrame, auto: pd.DataFrame) -> pd.DataFrame:
    """Add 'category' column — 'automated' overrides _cat_base where applicable.

    Uses a vectorised merge instead of a row-by-row apply, so it's O(n log n)
    regardless of DataFrame size.
    """
    expanded = expanded.copy()
    expanded["category"] = expanded["_cat_base"]
    if auto.empty:
        return expanded

    auto_keys = (
        auto[["case_id", "country_label", "device"]]
        .drop_duplicates()
        .assign(case_id=lambda d: d["case_id"].astype(int))
        .assign(_auto=True)
    )
    expanded["case_id"] = expanded["case_id"].astype(int)
    merged = expanded.merge(auto_keys, on=["case_id", "country_label", "device"], how="left")
    expanded.loc[merged["_auto"].fillna(False).to_numpy(), "category"] = "automated"
    return expanded


# ── stats ─────────────────────────────────────────────────────────────────────
def _stats(expanded: pd.DataFrame, auto: pd.DataFrame) -> dict:
    """Expanded row counts, unique case counts, and framework breakdown."""
    cats   = expanded["category"].value_counts()
    n_auto = int(cats.get("automated",      0))
    n_back = int(cats.get("backlog",         0))
    n_na   = int(cats.get("not_applicable",  0))
    total  = len(expanded)

    def _u(cat: str) -> int:
        return int(expanded[expanded["category"] == cat]["case_id"].nunique())

    u_total = int(expanded["case_id"].nunique())
    u_auto  = _u("automated")
    u_back  = _u("backlog")
    u_na    = _u("not_applicable")

    # Framework breakdown via merge (vectorised — no row-by-row apply)
    n_java = u_java = n_testim = u_testim = 0
    if not auto.empty and n_auto > 0:
        auto_exp = expanded[expanded["category"] == "automated"].copy()
        auto_exp["case_id"] = auto_exp["case_id"].astype(int)

        base = auto[["case_id", "country_label", "device", "framework"]].copy()
        base["case_id"] = base["case_id"].astype(int)

        for fw_name, fw_mask in [
            ("java",   base["framework"] == "java"),
            ("testim", base["framework"].isin(["testim_desktop", "testim_mobile"])),
        ]:
            keys = (
                base[fw_mask][["case_id", "country_label", "device"]]
                .drop_duplicates()
                .assign(**{f"_{fw_name}": True})
            )
            m    = auto_exp.merge(keys, on=["case_id", "country_label", "device"], how="left")
            flag = f"_{fw_name}"
            if fw_name == "java":
                n_java   = int(m[flag].sum())
                u_java   = int(m[m[flag] == True]["case_id"].nunique())
            else:
                n_testim = int(m[flag].sum())
                u_testim = int(m[m[flag] == True]["case_id"].nunique())

    automatable = n_auto + n_back
    scoped      = n_auto + n_back + n_na

    return {
        "total":           total,    "u_total":   u_total,
        "automated":       n_auto,   "u_auto":    u_auto,
        "java":            n_java,   "u_java":    u_java,
        "testim":          n_testim, "u_testim":  u_testim,
        "backlog":         n_back,   "u_back":    u_back,
        "not_applicable":  n_na,     "u_na":      u_na,
        "cov_total":       n_auto / total        * 100 if total        else 0.0,
        "cov_automatable": n_auto / automatable  * 100 if automatable  else 0.0,
        "na_pct":          n_na   / scoped        * 100 if scoped       else 0.0,
    }


# ── summary table ─────────────────────────────────────────────────────────────
def _build_summary(
    scope_data: dict[str, tuple],
) -> pd.DataFrame:
    """Build the summary table from pre-loaded scope data.

    *scope_data* maps scope → (raw, auto, rules) already filtered to that scope.
    """
    # Pre-compute and cache expanded DataFrames so _detail_view can reuse them
    # without running _expand_baseline / _classify_expanded a second time.
    expanded_cache: dict[tuple, pd.DataFrame] = {}
    scope_data["_expanded_cache"] = expanded_cache  # type: ignore[assignment]

    rows = []
    for bu, scope in _scoped_bus():
        if scope not in scope_data:
            continue
        raw_all, auto_all, rules_all = scope_data[scope]
        raw, auto, rules = _filter_bu(raw_all, auto_all, rules_all, bu)
        if raw.empty:
            continue
        expanded = _expand_baseline(raw, rules)
        if expanded.empty:
            continue
        expanded = _classify_expanded(expanded, auto)
        expanded_cache[(bu, scope)] = expanded   # cache for detail view
        s = _stats(expanded, auto)
        rows.append({
            "BU":        bu,
            "Scope":     "Next Gen" if scope == "next_gen" else "Website",
            "Total":     s["total"],
            "Automated": s["automated"],
            "Java":      s["java"],
            "TestIM":    s["testim"],
            "Backlog":   s["backlog"],
            "N/A":       s["not_applicable"],
            "Cov. %":    round(s["cov_total"], 1),
        })
    return pd.DataFrame(rows)


# ── detail view ───────────────────────────────────────────────────────────────
def _metric_pair(col, label: str, n: int, u: int, help: str = "") -> None:
    """Metric card showing expanded count + unique case count caption."""
    col.metric(label, f"{n:,}", help=help or None)
    col.caption(f"{u:,} {'case' if u == 1 else 'cases'}")


def _baseline_pivot(expanded: pd.DataFrame, key_prefix: str) -> None:
    """Interactive pivot over the full regression baseline (all categories)."""
    st.markdown("##### 📊 Pivot")
    if expanded.empty:
        return

    # Build display DataFrame
    disp = expanded[["case_id", "country_label", "device", "category"]].copy()
    disp["Country"]  = disp["country_label"].map(lambda c: COUNTRY_NAMES.get(c, c))
    disp["Device"]   = disp["device"]
    disp["Category"] = disp["category"].map({
        "automated":      "Automated",
        "backlog":        "Backlog",
        "not_applicable": "N/A",
    }).fillna("Other")

    available = ["Country", "Device"]

    c1, c2 = st.columns(2)
    row_sel = c1.multiselect(
        "Rows", available, default=["Country"], key=f"{key_prefix}_bl_rows"
    )
    remaining = [d for d in available if d not in row_sel]
    col_sel = c2.multiselect(
        "Columns", remaining, default=remaining, key=f"{key_prefix}_bl_cols"
    )

    if not row_sel:
        st.caption("Select at least one row dimension.")
        return

    col_dims = col_sel + ["Category"]

    try:
        pv = pd.pivot_table(
            disp,
            values="case_id",
            index=row_sel,
            columns=col_dims,
            aggfunc="count",
            fill_value=0,
            margins=True,
            margins_name="Total",
        )
        st.dataframe(pv, use_container_width=True)
    except Exception as exc:
        st.error(f"Pivot error: {exc}")


def _detail_view(bu: str, scope: str, scope_data: dict[str, tuple]) -> None:
    if scope not in scope_data:
        st.info("No data loaded for this scope.")
        return
    raw_all, auto_all, rules_all = scope_data[scope]
    raw, auto, rules = _filter_bu(raw_all, auto_all, rules_all, bu)
    if raw.empty:
        st.info("No cases found.")
        return

    # Re-use pre-computed expanded data cached in scope_data if available,
    # otherwise compute it now (avoids double work vs _build_summary).
    _cache = scope_data.get("_expanded_cache", {})
    cache_key = (bu, scope)
    if cache_key in _cache:
        expanded = _cache[cache_key]
    else:
        expanded = _classify_expanded(_expand_baseline(raw, rules), auto)

    if expanded.empty:
        st.info(
            "No big_regr cases found for this BU. "
            "Check that cases have the 'big_regr_desktop' / 'big_regr_mobile' label. "
            "If you just labelled them, click 🔄 Refresh."
        )
        return

    s = _stats(expanded, auto)

    # ── Row 1: Total · Automated · Backlog · N/A ─────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    _metric_pair(c1, "Total (baseline)", s["total"],         s["u_total"])
    _metric_pair(
        c2, "Automated", s["automated"], s["u_auto"],
        help=f"{s['cov_total']:.1f}% of total · {s['cov_automatable']:.1f}% of automatable",
    )
    _metric_pair(c3, "Backlog",          s["backlog"],        s["u_back"])
    _metric_pair(
        c4, "Not Applicable", s["not_applicable"], s["u_na"],
        help=f"{s['na_pct']:.1f}% of scoped rows",
    )

    # ── Coverage line (with N/A %) ────────────────────────────────────────────
    st.markdown(
        f"**Coverage vs total:** `{s['cov_total']:.1f}%` &nbsp;·&nbsp; "
        f"**Coverage vs automatable:** `{s['cov_automatable']:.1f}%` &nbsp;·&nbsp; "
        f"**Not Applicable:** `{s['na_pct']:.1f}%`",
        unsafe_allow_html=True,
    )

    st.divider()

    # ── Row 2: Framework breakdown ────────────────────────────────────────────
    st.markdown("##### Automated by framework")
    f1, f2, f3 = st.columns(3)
    _metric_pair(f1, "Java",   s["java"],   s["u_java"])
    _metric_pair(f2, "TestIM", s["testim"], s["u_testim"])

    java_pct   = s["java"]   / s["automated"] * 100 if s["automated"] else 0.0
    testim_pct = s["testim"] / s["automated"] * 100 if s["automated"] else 0.0
    f3.markdown(
        f"<div style='padding-top:8px;font-size:13px;color:#5e6677'>"
        f"Java &nbsp;<b>{java_pct:.1f}%</b><br>"
        f"TestIM &nbsp;<b>{testim_pct:.1f}%</b><br>"
        f"<span style='font-size:11px'>"
        f"(% of automated rows — may sum &gt;100% if both frameworks cover the same row)"
        f"</span></div>",
        unsafe_allow_html=True,
    )
    st.divider()

    # ── Pivot ─────────────────────────────────────────────────────────────────
    bu_key = bu.lower().replace(" ", "_")
    _baseline_pivot(expanded, key_prefix=f"bl_{bu_key}_{scope}")



# ── render ────────────────────────────────────────────────────────────────────
def render() -> None:
    st.subheader("📋 Backlog & Coverage")
    st.caption(
        "Baseline: cases with the label 'big_regr_desktop' / 'big_regr_mobile'. "
    )

    with st.spinner("Computing backlog stats…"):
        scope_data: dict[str, tuple] = {}
        for scope in ("website", "next_gen"):
            raw, auto, rules = _load_scope(scope)
            if not raw.empty:
                scope_data[scope] = (raw, auto, rules)
        summary = _build_summary(scope_data)

    if summary.empty:
        st.warning(
            "No baseline data found. Ensure cases have the big_regr_desktop / "
            "big_regr_mobile labels in TestRail, then click 🔄 Refresh."
        )
        return

    # ── Summary table ─────────────────────────────────────────────────────────
    st.markdown("#### All Business Units")
    st.dataframe(
        summary,
        use_container_width=True,
        hide_index=True,
        column_config={
            "BU":        st.column_config.TextColumn("Business Unit", width="medium"),
            "Scope":     st.column_config.TextColumn("Scope",         width="small"),
            "Total":     st.column_config.NumberColumn("Total"),
            "Automated": st.column_config.NumberColumn("Automated"),
            "Java":      st.column_config.NumberColumn("Java"),
            "TestIM":    st.column_config.NumberColumn("TestIM"),
            "Backlog":   st.column_config.NumberColumn("Backlog"),
            "N/A":       st.column_config.NumberColumn("N/A"),
            "Cov. %":    st.column_config.NumberColumn("Coverage %", format="%.1f%%"),
        },
    )

    st.divider()

    # ── Detail ────────────────────────────────────────────────────────────────
    st.markdown("#### Detail by Business Unit")

    all_pairs   = _scoped_bus()
    pair_labels = [
        f"{bu} (Next Gen)" if sc == "next_gen" else bu
        for bu, sc in all_pairs
    ]
    pair_map    = dict(zip(pair_labels, all_pairs))

    # Only show BUs that have data
    summary_bus = set(summary["BU"].tolist())
    available   = [lbl for lbl in pair_labels if pair_map[lbl][0] in summary_bus]

    if not available:
        return

    choice_lbl = st.selectbox(
        "Business Unit", available, key="bl_bu_detail",
        label_visibility="collapsed",
    )
    chosen_bu, chosen_scope = pair_map[choice_lbl]
    _detail_view(chosen_bu, chosen_scope, scope_data)
