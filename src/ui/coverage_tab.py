"""Coverage tab — automation coverage per functional area (TestRail section).

Output mirrors the manual "coverage_outputs_<BU>.xlsx" Chiara produces:
  * Section names normalised by auto-stripping dominant "container roots"
    (e.g. "SD" or "WTR > Root") so the rows match the Excel "Main Category".
  * Desktop / Mobile / Unspecified columns count EXPANDED rows (same convention
    as Explorer / Report) — a case automated for both devices counts twice.
  * Coverage % uses unique case_ids so it stays a proper "% of cases covered".

Two stacked views per BU
────────────────────────
  1. **All Automated Cases** — coverage over the full non-deprecated universe.
  2. **No-Regression Baseline Only** — same layout but restricted to cases
     tagged with `big_regr_desktop` / `big_regr_mobile` (the regression baseline
     used by the Backlog tab), with device-specific label matching.

The two views share the same renderer (`_render_coverage_section`) so the
layout is identical — only the input subset changes.

Layout per view
───────────────
  * Headline metrics: Total · Automated unique · Automated rows (D+M) · Coverage %
  * Granularity slider (0 = Main Category, 1 = Secondary, 2-3 = deeper)
  * Table — Area | Total | Desktop | Mobile [| Unspecified] | Automated | Coverage %
  * Pie chart — share of automated rows per area
  * Bar chart — coverage % per area (sorted, zero rows pushed to the bottom)
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from ..bu_rules import ALL_RULES
from ..rules_engine import evaluate_rules
from . import theme

# ── brand chart colors (constant across light/dark) ──────────────────────────
_PIE_PALETTE = [
    "#ED7D31", "#4472C4", "#70AD47", "#FFC000", "#7030A0",
    "#C00000", "#00B0F0", "#A5A5A5", "#264478", "#9E480E",
    "#636363", "#997300", "#43682B", "#255E91", "#698ED0",
]


# ── data loading ─────────────────────────────────────────────────────────────
def _scope_label(scope: str) -> str:
    return {"website": "🌐 Website", "mobile_app": "📱 Mobile App",
            "next_gen": "🧩 Next Gen"}.get(scope, scope)


def _bus_for_scope(scope: str) -> list[str]:
    return sorted({r.bu for r in ALL_RULES if r.scope == scope})


def _load_scope(scope: str):
    """Cached evaluate_rules call shared with other tabs."""
    rules = [r for r in ALL_RULES if r.scope == scope]
    if not rules:
        return None, None, []
    result = evaluate_rules(tuple(r.name for r in rules))
    return result.raw_cases, result.automated, rules


# ── section helpers ──────────────────────────────────────────────────────────
def _split_path(path: str) -> list[str]:
    return [p.strip() for p in (path or "").split(">") if p.strip()]


def _detect_container_chain(
    paths: pd.Series, dominance: float = 0.8, max_depth: int = 5,
) -> list[str]:
    """Detect the chain of dominant "container" sections at the root.

    A component is a container if it holds more than *dominance* (e.g. 80%) of
    cases at its depth.  Returns the ordered chain.

    Example: SD suite has 99% of cases under "SD" → ["SD"].  Once stripped, the
    next level ("Checkout", "Customer", ...) is balanced and we stop.

    WTR suite has level-1 already balanced (Checkout, PIM, ...) → returns [].
    """
    if paths is None or paths.empty:
        return []
    parts_list = paths.fillna("").map(_split_path)
    chain: list[str] = []
    current = parts_list
    for _ in range(max_depth):
        first = current.map(lambda p: p[0] if p else None).dropna()
        if first.empty:
            break
        counts = first.value_counts()
        top, top_n = counts.index[0], counts.iloc[0]
        if top_n / len(first) < dominance:
            break
        chain.append(str(top))
        current = current.map(
            lambda p: p[1:] if (p and p[0] == top) else None
        ).dropna()
        if current.empty:
            break
    return chain


def _section_for_path(path: str, chain: list[str], offset: int = 0) -> str:
    """Return the area label for *path*, stripping known container chain.

    *offset* lets the user drill down further (0 = main category, 1 = secondary).
    Paths that do not start with the chain (e.g. sibling folders like "Test folder")
    are kept as-is and grouped under their own first component.
    """
    parts = _split_path(path)
    if not parts:
        return "(root)"
    # Strip matching container prefix only (preserves siblings like "Test folder")
    i = 0
    while i < len(chain) and i < len(parts) and parts[i] == chain[i]:
        i += 1
    remaining = parts[i:]
    if not remaining:
        # Case sat directly at the container — surface the last chain component
        return chain[-1] if chain else "(root)"
    take = min(offset + 1, len(remaining))
    return " > ".join(remaining[:take])


# ── coverage table ───────────────────────────────────────────────────────────
def _coverage_table(
    non_dep: pd.DataFrame,
    auto_bu: pd.DataFrame,
    auto_ids: set[int],
    depth_offset: int = 0,
) -> tuple[pd.DataFrame, list[str]]:
    """Aggregate per-section counts after smart container-chain stripping.

    Parameters
    ----------
    non_dep
        Non-deprecated cases for the chosen BU (already filtered).
    auto_bu
        Expanded automated rows for the chosen BU.
    auto_ids
        Pre-computed set of automated case_ids (saves a `set()` build per render).
    depth_offset
        0 = main category (first level after the auto-detected container chain),
        1 = secondary, etc.

    Returns
    -------
    (df, container_chain)
        df columns: section, total, desktop, mobile, unspecified,
                    automated, auto_unique, coverage_pct
        container_chain : the auto-stripped roots (for display).
    """
    if non_dep.empty:
        return pd.DataFrame(), []

    chain = _detect_container_chain(non_dep["section_path"])

    work = non_dep[["case_id", "section_path"]].copy()
    work["section"] = work["section_path"].fillna("").map(
        lambda p: _section_for_path(p, chain, depth_offset)
    )
    work["_is_auto"] = work["case_id"].isin(auto_ids)

    grouped = (
        work.groupby("section", dropna=False)
        .agg(total=("case_id", "nunique"),
             auto_unique=("_is_auto", "sum"))
        .reset_index()
    )
    grouped["auto_unique"] = grouped["auto_unique"].astype(int)

    # Desktop / Mobile / Unspecified EXPANDED row counts.
    # Slice to just the 3 columns we need — avoids copying the full auto_bu DataFrame
    # (which carries ~20 columns) just to add the "section" derived column.
    desktop_map:     dict[str, int] = {}
    mobile_map:      dict[str, int] = {}
    unspecified_map: dict[str, int] = {}
    if not auto_bu.empty and "section_path" in auto_bu.columns:
        ap = auto_bu[["section_path", "device"]].copy()
        ap["section"] = ap["section_path"].fillna("").map(
            lambda p: _section_for_path(p, chain, depth_offset)
        )
        dev_grp = ap.groupby(["section", "device"]).size().unstack(fill_value=0)
        for dev_name, target in [("Desktop", desktop_map),
                                  ("Mobile",  mobile_map),
                                  ("Unspecified", unspecified_map)]:
            if dev_name in dev_grp.columns:
                target.update(dev_grp[dev_name].to_dict())

    grouped["desktop"]     = grouped["section"].map(desktop_map).fillna(0).astype(int)
    grouped["mobile"]      = grouped["section"].map(mobile_map).fillna(0).astype(int)
    grouped["unspecified"] = grouped["section"].map(unspecified_map).fillna(0).astype(int)
    grouped["automated"]   = (
        grouped["desktop"] + grouped["mobile"] + grouped["unspecified"]
    )
    grouped["coverage_pct"] = (
        (grouped["auto_unique"] / grouped["total"] * 100)
        .round(1).fillna(0.0)
    )

    # Sort: non-zero automated first (by automated desc), then zero rows at the bottom
    # (by total desc, so the biggest "empty" areas float to the top of the zero block).
    grouped["_zero_flag"] = (grouped["automated"] == 0).astype(int)
    grouped = grouped.sort_values(
        by=["_zero_flag", "automated", "total"],
        ascending=[True, False, False],
    ).drop(columns=["_zero_flag"]).reset_index(drop=True)

    return grouped[["section", "total", "desktop", "mobile", "unspecified",
                    "automated", "auto_unique", "coverage_pct"]], chain


# ── charts ───────────────────────────────────────────────────────────────────
def _area_color_map(cov: pd.DataFrame) -> dict[str, str]:
    """Stable area → palette-color mapping, shared between pie and bar charts.

    Areas are ordered by automated DESC first (so the biggest slice gets the
    first palette color, then the second-biggest gets the second, etc.).  This
    keeps colors consistent across both charts even though the bar chart sorts
    by coverage %.
    """
    ordered = cov.sort_values("automated", ascending=False)["section"].tolist()
    return {area: _PIE_PALETTE[i % len(_PIE_PALETTE)] for i, area in enumerate(ordered)}


def _build_pie(cov: pd.DataFrame, color_map: dict[str, str]) -> alt.Chart | None:
    """Pie of automated case distribution across sections (slice size = automated)."""
    tc   = theme.colors()
    data = cov[cov["automated"] > 0].copy()
    if data.empty:
        return None
    sections_order = data.sort_values("automated", ascending=False)["section"].tolist()
    color_scale = alt.Scale(
        domain=sections_order,
        range=[color_map[s] for s in sections_order],
    )
    base = alt.Chart(data).encode(
        theta=alt.Theta("automated:Q", stack=True),
        color=alt.Color("section:N", scale=color_scale, legend=None,
                        sort=sections_order),
        order=alt.Order("automated:Q", sort="descending"),
        tooltip=[
            alt.Tooltip("section:N",      title="Area"),
            alt.Tooltip("total:Q",        title="Total cases", format=","),
            alt.Tooltip("desktop:Q",      title="Desktop",     format=","),
            alt.Tooltip("mobile:Q",       title="Mobile",      format=","),
            alt.Tooltip("automated:Q",    title="Automated",   format=","),
            alt.Tooltip("coverage_pct:Q", title="Coverage %",  format=".1f"),
        ],
    )
    arc = base.mark_arc(innerRadius=55, outerRadius=130,
                        stroke=tc["bg"], strokeWidth=2)
    return arc.properties(height=320).configure(background=tc["bg"])


def _build_coverage_bar(cov: pd.DataFrame, color_map: dict[str, str]) -> alt.Chart:
    """Horizontal bars: coverage % per section.

    Sort: zero-automated rows pushed to the bottom (same convention as the table).
    Colors: same per-area palette as the pie chart, so a colour means the same
    area in both views.
    """
    tc   = theme.colors()
    data = cov.copy()
    data["label"]     = data["coverage_pct"].map(lambda v: f"{v:.1f}%")
    # Sort key that mirrors the table: non-zero by coverage DESC, then zero rows
    # (sorted by total DESC so larger empty areas float to the top of the zero block).
    data["_sort_key"] = data.apply(
        lambda r: (0, -float(r["coverage_pct"]), -float(r["total"]))
        if r["automated"] > 0
        else (1, -float(r["total"]), 0.0),
        axis=1,
    )
    # Build the ordered list for the Y axis (Altair sorts categorical Y by an
    # explicit list).  Convert tuples to a deterministic stringified key so
    # Altair's sort uses the right order.
    y_order = data.sort_values("_sort_key").apply(
        lambda r: r["section"], axis=1,
    ).tolist()

    color_scale = alt.Scale(
        domain=list(color_map.keys()),
        range=[color_map[s] for s in color_map],
    )

    bars = (
        alt.Chart(data)
        .mark_bar(size=18, cornerRadiusEnd=3)
        .encode(
            x=alt.X("coverage_pct:Q",
                    scale=alt.Scale(domain=[0, 100]),
                    axis=alt.Axis(title="Coverage %", grid=True,
                                  gridColor=tc["grid"], labelColor=tc["axis_label"],
                                  titleColor=tc["axis_label"], domain=False)),
            y=alt.Y("section:N", sort=y_order,
                    axis=alt.Axis(title=None, labelLimit=240,
                                  labelColor=tc["axis_label"],
                                  domain=False, ticks=False)),
            color=alt.Color("section:N", scale=color_scale, legend=None),
            tooltip=[
                alt.Tooltip("section:N",      title="Area"),
                alt.Tooltip("total:Q",        title="Total cases",        format=","),
                alt.Tooltip("auto_unique:Q",  title="Automated (unique)", format=","),
                alt.Tooltip("coverage_pct:Q", title="Coverage %",         format=".1f"),
            ],
        )
    )
    text = (
        alt.Chart(data)
        .mark_text(align="left", dx=5, fontSize=10, color=tc["text_2"])
        .encode(
            x=alt.X("coverage_pct:Q"),
            y=alt.Y("section:N", sort=y_order),
            text=alt.Text("label:N"),
        )
    )

    return (
        alt.layer(bars, text)
        .properties(height=alt.Step(26))
        .configure(background=tc["bg"])
        .configure_view(stroke=tc["border_soft"], strokeWidth=1, fill=tc["bg"])
        .configure_axis(labelFont="Arial")
    )


# ── regression-baseline filter ───────────────────────────────────────────────
_LBL_REGR_DESK = "big_regr_desktop"
_LBL_REGR_MOB  = "big_regr_mobile"


def _filter_to_regression_baseline(
    non_dep: pd.DataFrame, auto_bu: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, set[int]]:
    """Filter both DataFrames to the regression baseline (the same one used by
    the Backlog tab): cases tagged with `big_regr_desktop` and/or
    `big_regr_mobile`.

    For `auto_bu`, the device must match the label:
      - Desktop rows kept only if the case has `big_regr_desktop`
      - Mobile  rows kept only if the case has `big_regr_mobile`
      - Unspecified (Next Gen) rows kept if the case has either label

    Returns (non_dep_baseline, auto_bu_baseline, baseline_auto_case_ids).
    """
    if non_dep.empty or "labels" not in non_dep.columns:
        return non_dep.iloc[0:0], auto_bu.iloc[0:0], set()

    has_d = non_dep["labels"].apply(
        lambda ls: _LBL_REGR_DESK in ls if isinstance(ls, list) else False
    )
    has_m = non_dep["labels"].apply(
        lambda ls: _LBL_REGR_MOB in ls if isinstance(ls, list) else False
    )
    nd_base = non_dep[has_d | has_m]
    if nd_base.empty or auto_bu.empty:
        return nd_base, auto_bu.iloc[0:0], set()

    base_d_ids = set(non_dep.loc[has_d, "case_id"].astype(int))
    base_m_ids = set(non_dep.loc[has_m, "case_id"].astype(int))
    base_all   = base_d_ids | base_m_ids

    ab          = auto_bu.copy()
    ab["case_id"] = ab["case_id"].astype(int)
    keep_d = (ab["device"] == "Desktop")     & ab["case_id"].isin(base_d_ids)
    keep_m = (ab["device"] == "Mobile")      & ab["case_id"].isin(base_m_ids)
    keep_u = (ab["device"] == "Unspecified") & ab["case_id"].isin(base_all)

    ab_base = ab[keep_d | keep_m | keep_u]
    return nd_base, ab_base, set(ab_base["case_id"].astype(int).unique())


# ── per-BU view ──────────────────────────────────────────────────────────────
def _render_coverage_section(
    non_dep: pd.DataFrame,
    auto_bu: pd.DataFrame,
    auto_ids: set[int],
    *,
    key_prefix: str,
    scope: str,
    show_tool_facet: bool = True,
) -> None:
    """Render the full coverage block (metrics + table + charts) for a subset.

    Pulled out of `_coverage_for` so the regression-baseline view can reuse the
    exact same layout without duplicating code.  *key_prefix* must be unique
    per call so Streamlit widgets don't collide.
    """
    if non_dep.empty:
        st.info("No cases in this subset.")
        return

    # ── headline metrics ──────────────────────────────────────────────────────
    auto_unique         = int(non_dep["case_id"].isin(auto_ids).sum())
    total               = int(non_dep["case_id"].nunique())
    cov_pct             = (auto_unique / total * 100) if total else 0.0
    auto_expanded_total = int(len(auto_bu))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total cases (non-deprecated)", f"{total:,}")
    c2.metric("Automated (unique)", f"{auto_unique:,}")
    c3.metric("Automated rows (D+M)", f"{auto_expanded_total:,}",
              help="Expanded rows: Desktop + Mobile. Same convention as Explorer / Report.")
    c4.metric("Coverage", f"{cov_pct:.1f}%")

    # ── section granularity ───────────────────────────────────────────────────
    st.markdown("")
    depth_offset = st.slider(
        "Granularity", 0, 3, 0,
        key=f"{key_prefix}_granularity",
        help=("0 = Main Category (auto-detected — strips dominant root containers "
              "like \"SD\" or \"WTR\"); 1 = Secondary; 2-3 = deeper sub-sections."),
    )

    cov, chain = _coverage_table(non_dep, auto_bu, auto_ids, depth_offset=depth_offset)
    if cov.empty:
        st.info("No sections to display.")
        return

    if chain:
        chain_str = " > ".join(f"`{c}`" for c in chain)
        st.caption(
            f"Auto-stripped container chain: {chain_str} "
            f"(dominant root folders that contain >80% of the cases)."
        )
    else:
        st.caption("No dominant container detected — sections shown at the top level.")

    # ── table ─────────────────────────────────────────────────────────────────
    st.markdown("#### 📋 Coverage table")
    display = cov.copy()
    # Add a Total row at the bottom (matching the Excel format)
    total_row = pd.DataFrame([{
        "section":      "Total",
        "total":        int(cov["total"].sum()),
        "desktop":      int(cov["desktop"].sum()),
        "mobile":       int(cov["mobile"].sum()),
        "unspecified":  int(cov["unspecified"].sum()),
        "automated":    int(cov["automated"].sum()),
        "auto_unique":  int(cov["auto_unique"].sum()),
        "coverage_pct": cov_pct,
    }])
    display = pd.concat([display, total_row], ignore_index=True)

    # Only show Unspecified column if any value is non-zero (typically Next Gen)
    show_unspecified = bool(display["unspecified"].sum() > 0)
    cols = ["section", "total", "desktop", "mobile"]
    if show_unspecified:
        cols.append("unspecified")
    cols += ["automated", "coverage_pct"]

    auto_label = "Automated (D+M+U)" if show_unspecified else "Automated (D+M)"

    st.dataframe(
        display[cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "section":      st.column_config.TextColumn(
                "Main Category" if depth_offset == 0
                else ("Secondary Category" if depth_offset == 1
                      else f"Area (depth +{depth_offset})"),
                width="large"),
            "total":        st.column_config.NumberColumn("Total cases"),
            "desktop":      st.column_config.NumberColumn("Desktop"),
            "mobile":       st.column_config.NumberColumn("Mobile"),
            "unspecified":  st.column_config.NumberColumn("Unspecified"),
            "automated":    st.column_config.NumberColumn(
                auto_label,
                help="Sum of expanded rows per device.  Matches the Excel \"Total\" column."),
            "coverage_pct": st.column_config.ProgressColumn(
                "Coverage %", format="%.1f%%", min_value=0, max_value=100,
                help="Unique automated cases ÷ Total cases per area."),
        },
    )

    # ── charts ────────────────────────────────────────────────────────────────
    # Build one color map shared by both charts so a given area is always the
    # same color across pie + bar.
    color_map = _area_color_map(cov)

    st.markdown("")
    left, right = st.columns([1, 1.2], gap="large")
    with left:
        st.markdown("##### 🥧 Automated distribution")
        st.caption("Share of automated rows per area (slice size = Desktop + Mobile)")
        pie = _build_pie(cov, color_map)
        if pie is None:
            st.info("No automated cases yet.")
        else:
            st.altair_chart(pie, use_container_width=True)

    with right:
        st.markdown("##### 📊 Coverage % per area")
        st.caption("Sorted by coverage % (zero-automated areas pushed to the bottom). "
                   "Colors match the pie chart — same color = same area.")
        bar = _build_coverage_bar(cov, color_map)
        st.altair_chart(bar, use_container_width=True)

    # ── mobile-app facet (only shown in the full view to avoid duplication) ──
    if show_tool_facet and scope == "mobile_app" and not auto_bu.empty \
            and "automation_tool" in auto_bu.columns:
        st.divider()
        st.markdown("##### 🛠 Automated cases by automation tool")
        tool = (
            auto_bu.dropna(subset=["automation_tool"])
            .drop_duplicates(subset=["case_id"])
            .groupby("automation_tool").size()
            .reset_index(name="count")
        )
        if not tool.empty:
            st.dataframe(tool, use_container_width=True, hide_index=True)
        else:
            st.caption("No `Automation Tool` values populated on matching cases.")


def _coverage_for(scope: str, bu_choice: str) -> None:
    raw, auto, rules = _load_scope(scope)
    if raw is None or raw.empty:
        st.info("No data loaded for this scope.")
        return

    # `rules` is already filtered to *scope* by _load_scope, so no second scope check.
    bu_suites = {r.suite_id for r in rules if r.bu == bu_choice}

    raw_bu  = raw[raw["suite_id"].isin(bu_suites)]
    auto_bu = auto[auto["bu"] == bu_choice] if not auto.empty else auto

    if raw_bu.empty:
        st.info(f"No cases found for **{bu_choice}**.")
        return

    non_dep  = raw_bu[raw_bu["deprecated"] == False]  # noqa: E712
    auto_ids = set(auto_bu["case_id"].unique()) if not auto_bu.empty else set()

    # ── View 1: full universe ────────────────────────────────────────────────
    st.markdown("### 🌐 All Automated Cases")
    st.caption(
        "Coverage over the full universe of non-deprecated cases for this BU. "
        "Useful for the broadest picture of automation reach."
    )
    _render_coverage_section(
        non_dep, auto_bu, auto_ids,
        key_prefix=f"cov_full_{scope}_{bu_choice}",
        scope=scope,
        show_tool_facet=True,
    )

    st.divider()

    # ── View 2: regression baseline only ─────────────────────────────────────
    st.markdown("### 📋 No-Regression Baseline Only")
    st.caption(
        "Same breakdown, restricted to cases with the **`big_regr_desktop`** / "
        "**`big_regr_mobile`** labels — the regression baseline used by the "
        "Backlog tab.  Desktop / Mobile rows are matched against the label per "
        "device, so this view tells you the *effective* coverage of what is "
        "actually scheduled for regression."
    )
    nd_base, ab_base, ids_base = _filter_to_regression_baseline(non_dep, auto_bu)
    if nd_base.empty:
        st.info(
            "No cases tagged with `big_regr_desktop` / `big_regr_mobile` for this BU. "
            "Add the labels in TestRail (or click 🔄 Refresh if you just did)."
        )
        return
    _render_coverage_section(
        nd_base, ab_base, ids_base,
        key_prefix=f"cov_regr_{scope}_{bu_choice}",
        scope=scope,
        show_tool_facet=False,   # already shown in the full view
    )


# ── render ───────────────────────────────────────────────────────────────────
def render() -> None:
    st.subheader("📐 Coverage by Area")
    st.caption(
        "Automation coverage broken down by functional area (TestRail section). "
        "Counts use the same convention as the other tabs: Desktop + Mobile "
        "expanded rows for the automation totals, unique case IDs for the % coverage."
    )

    # ── scope selector ───────────────────────────────────────────────────────
    scopes = ["website", "mobile_app", "next_gen"]
    scope_labels = [_scope_label(s) for s in scopes]
    label_to_scope = dict(zip(scope_labels, scopes))

    c1, c2 = st.columns([1, 2])
    chosen_label = c1.radio(
        "Scope", scope_labels, horizontal=True, key="cov_scope",
        label_visibility="collapsed",
    )
    chosen_scope = label_to_scope[chosen_label]

    bus = _bus_for_scope(chosen_scope)
    if not bus:
        st.info("No rules defined for this scope.")
        return

    if len(bus) > 1:
        bu_choice = c2.selectbox(
            "Business Unit", bus, key=f"cov_bu_{chosen_scope}",
            label_visibility="collapsed",
        )
    else:
        bu_choice = bus[0]
        c2.markdown(f"**{bu_choice}**")

    st.divider()
    _coverage_for(chosen_scope, bu_choice)
