"""Coverage tab — automation coverage per functional area (TestRail section).

Output mirrors the manual "coverage_outputs_<BU>.xlsx" Chiara produces:
  * Section names stripped of the BU root prefix (e.g. "SD > X" → "X")
  * Columns Desktop / Mobile / Total counted as EXPANDED rows (same convention
    as Explorer / Report tabs) so a case automated for both devices counts twice
  * Coverage % uses unique case_ids so it stays a proper "% of cases covered"

Layout
──────
  * Scope (radio) + BU (dropdown) on the top row
  * Headline metrics: Total · Automated unique · Coverage %
  * Section depth slider (default 1 = Main Category)
  * Table — Area | Total | Desktop | Mobile | Automated | Coverage %
  * Pie chart — share of automated cases per area
  * Bar chart — coverage % per area (sorted, traffic-light colored)
"""
from __future__ import annotations

import re

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
def _detect_common_prefix(paths: pd.Series) -> str:
    """If all non-empty paths share a common first component, return it.  Else ''."""
    if paths is None or paths.empty:
        return ""
    s = paths.fillna("").str.strip()
    s = s[s.str.len() > 0]
    if s.empty:
        return ""
    first_parts = s.str.split(">").str[0].str.strip()
    if first_parts.nunique() != 1:
        return ""
    return str(first_parts.iloc[0])


def _strip_prefix(path: str, prefix: str) -> str:
    if not prefix:
        return path or ""
    pat = rf"^{re.escape(prefix)}\s*>\s*"
    out = re.sub(pat, "", path or "")
    # A case sitting directly under the root → use the prefix itself as section
    return out if out else prefix


def _take_levels(path: str, n: int) -> str:
    parts = [p.strip() for p in (path or "").split(">") if p.strip()]
    if not parts:
        return "(root)"
    return " > ".join(parts[:n])


# ── coverage table ───────────────────────────────────────────────────────────
def _coverage_table(
    raw_bu: pd.DataFrame, auto_bu: pd.DataFrame, level: int,
) -> tuple[pd.DataFrame, str]:
    """Aggregate per-section counts.

    Returns
    -------
    (df, prefix)
        df columns: section, total, desktop, mobile, automated, coverage_pct
        prefix    : the BU root that was stripped (for display).
    """
    if raw_bu.empty:
        return pd.DataFrame(), ""

    non_dep = raw_bu[raw_bu["deprecated"] == False].copy()  # noqa: E712
    prefix  = _detect_common_prefix(non_dep["section_path"])

    non_dep["section"] = (
        non_dep["section_path"].fillna("")
        .map(lambda p: _strip_prefix(p, prefix))
        .map(lambda p: _take_levels(p, level))
    )

    auto_ids = set(auto_bu["case_id"].unique()) if not auto_bu.empty else set()
    non_dep["_is_auto"] = non_dep["case_id"].isin(auto_ids)

    grouped = (
        non_dep.groupby("section", dropna=False)
        .agg(total=("case_id", "nunique"),
             auto_unique=("_is_auto", "sum"))
        .reset_index()
    )
    grouped["auto_unique"] = grouped["auto_unique"].astype(int)

    # Desktop / Mobile / Unspecified EXPANDED row counts (same convention as
    # other tabs).  Unspecified covers Next Gen rules that don't split by device.
    desktop_map:     dict[str, int] = {}
    mobile_map:      dict[str, int] = {}
    unspecified_map: dict[str, int] = {}
    if not auto_bu.empty and "section_path" in auto_bu.columns:
        ap = auto_bu.copy()
        ap["section"] = (
            ap["section_path"].fillna("")
            .map(lambda p: _strip_prefix(p, prefix))
            .map(lambda p: _take_levels(p, level))
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
    )  # matches Excel "Total"
    grouped["coverage_pct"] = (
        (grouped["auto_unique"] / grouped["total"] * 100)
        .round(1).fillna(0.0)
    )

    grouped = grouped.sort_values("automated", ascending=False).reset_index(drop=True)
    return grouped[["section", "total", "desktop", "mobile", "unspecified",
                    "automated", "auto_unique", "coverage_pct"]], prefix


# ── charts ───────────────────────────────────────────────────────────────────
def _build_pie(cov: pd.DataFrame) -> alt.Chart | None:
    """Pie of automated case distribution across sections (slice size = automated)."""
    tc   = theme.colors()
    data = cov[cov["automated"] > 0].copy()
    if data.empty:
        return None
    sections_order = data.sort_values("automated", ascending=False)["section"].tolist()
    color_scale = alt.Scale(
        domain=sections_order,
        range=_PIE_PALETTE * (1 + len(sections_order) // len(_PIE_PALETTE)),
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


def _build_coverage_bar(cov: pd.DataFrame) -> alt.Chart:
    """Horizontal bars: coverage % per section, sorted descending."""
    tc   = theme.colors()
    data = cov.copy()
    data["label"]  = data["coverage_pct"].map(lambda v: f"{v:.1f}%")

    def _bucket(v: float) -> str:
        if v >= 80: return "high"
        if v >= 50: return "medium"
        return "low"
    data["bucket"] = data["coverage_pct"].map(_bucket)

    color_scale = alt.Scale(
        domain=["high", "medium", "low"],
        range=["#70AD47", "#ED7D31", "#C00000"],
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
            y=alt.Y("section:N",
                    sort=alt.EncodingSortField(field="coverage_pct", order="descending"),
                    axis=alt.Axis(title=None, labelLimit=240,
                                  labelColor=tc["axis_label"],
                                  domain=False, ticks=False)),
            color=alt.Color("bucket:N", scale=color_scale, legend=None),
            tooltip=[
                alt.Tooltip("section:N",      title="Area"),
                alt.Tooltip("total:Q",        title="Total cases", format=","),
                alt.Tooltip("auto_unique:Q",  title="Automated (unique)", format=","),
                alt.Tooltip("coverage_pct:Q", title="Coverage %",  format=".1f"),
            ],
        )
    )
    text = (
        alt.Chart(data)
        .mark_text(align="left", dx=5, fontSize=10, color=tc["text_2"])
        .encode(
            x=alt.X("coverage_pct:Q"),
            y=alt.Y("section:N",
                    sort=alt.EncodingSortField(field="coverage_pct", order="descending")),
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


# ── per-BU view ──────────────────────────────────────────────────────────────
def _coverage_for(scope: str, bu_choice: str) -> None:
    raw, auto, rules = _load_scope(scope)
    if raw is None or raw.empty:
        st.info("No data loaded for this scope.")
        return

    bu_to_suites: dict[str, set[int]] = {}
    for r in rules:
        if r.scope == scope:
            bu_to_suites.setdefault(r.bu, set()).add(r.suite_id)

    raw_bu  = raw[raw["suite_id"].isin(bu_to_suites.get(bu_choice, set()))]
    auto_bu = auto[auto["bu"] == bu_choice] if not auto.empty else auto

    if raw_bu.empty:
        st.info(f"No cases found for **{bu_choice}**.")
        return

    # ── headline metrics ──────────────────────────────────────────────────────
    non_dep  = raw_bu[raw_bu["deprecated"] == False]  # noqa: E712
    auto_ids = set(auto_bu["case_id"].unique()) if not auto_bu.empty else set()
    auto_unique = int(non_dep["case_id"].isin(auto_ids).sum()) if not non_dep.empty else 0
    total       = int(non_dep["case_id"].nunique()) if not non_dep.empty else 0
    cov_pct     = (auto_unique / total * 100) if total else 0.0
    auto_expanded_total = int(len(auto_bu)) if not auto_bu.empty else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total cases (non-deprecated)", f"{total:,}")
    c2.metric("Automated (unique)", f"{auto_unique:,}")
    c3.metric("Automated rows (D+M)", f"{auto_expanded_total:,}",
              help="Expanded rows: Desktop + Mobile. Same convention as Explorer / Report.")
    c4.metric("Coverage", f"{cov_pct:.1f}%")

    # ── section depth ─────────────────────────────────────────────────────────
    st.markdown("")
    level = st.slider(
        "Section depth", 1, 4, 1,
        key=f"cov_depth_{scope}_{bu_choice}",
        help=("Depth of section hierarchy (after stripping the BU root). "
              "Level 1 = Main Category, 2 = Secondary, etc."),
    )

    cov, prefix = _coverage_table(raw_bu, auto_bu, level=level)
    if cov.empty:
        st.info("No sections to display.")
        return

    if prefix:
        st.caption(f"Showing sections under the BU root `{prefix}` (root prefix stripped).")

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
                "Area" if level == 1 else "Area (depth %d)" % level,
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
    st.markdown("")
    left, right = st.columns([1, 1.2], gap="large")
    with left:
        st.markdown("##### 🥧 Automated distribution")
        st.caption("Share of automated rows per area (slice size = Desktop + Mobile)")
        pie = _build_pie(cov)
        if pie is None:
            st.info("No automated cases yet.")
        else:
            st.altair_chart(pie, use_container_width=True)

    with right:
        st.markdown("##### 📊 Coverage % per area")
        st.caption("Sorted by coverage. Red < 50% · Orange 50-80% · Green ≥ 80%")
        bar = _build_coverage_bar(cov)
        st.altair_chart(bar, use_container_width=True)

    # ── mobile-app facet ─────────────────────────────────────────────────────
    if scope == "mobile_app" and not auto_bu.empty and "automation_tool" in auto_bu.columns:
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
