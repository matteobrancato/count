from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import metrics
from ..bu_rules import ALL_RULES
from ..rules_engine import evaluate_rules


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
            checked = c1.checkbox("", value=True, key=f"{key_prefix}_bu_{bu}",
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
def _metric_card(title: str, subset: pd.DataFrame, accent: str) -> None:
    tot = metrics.totals(subset)
    st.markdown(
        f"""
        <div style="
            padding:18px 22px;border-radius:14px;
            background:linear-gradient(135deg,{accent}22,{accent}0a);
            border:1px solid {accent}44;margin-bottom:8px">
            <div style="font-size:13px;color:#5e6677;text-transform:uppercase;
                        letter-spacing:0.06em;font-weight:600">{title}</div>
            <div style="font-size:34px;font-weight:700;color:#1a1f36;margin-top:2px">
                {tot['total']:,}
            </div>
            <div style="font-size:13px;color:#5e6677;margin-top:4px">
                🖥 Desktop <b>{tot['desktop']:,}</b>  ·  📱 Mobile <b>{tot['mobile']:,}</b>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.expander("Breakdown", expanded=False):
        t1, t2, t3 = st.tabs(["By BU", "By Country", "By Device"])
        with t1:
            st.dataframe(metrics.breakdown_by(subset, ["bu"]),
                         use_container_width=True, hide_index=True)
        with t2:
            st.dataframe(metrics.breakdown_by(subset, ["bu", "country_label"]),
                         use_container_width=True, hide_index=True)
        with t3:
            st.dataframe(metrics.breakdown_by(subset, ["bu", "country_label", "device"]),
                         use_container_width=True, hide_index=True)


# --------------------------------------------------------------------- sub-views
def _coverage_view(scope: str, label: str, result=None) -> None:
    """Render coverage for *scope*.  Pass *result* to reuse an already-computed
    ExpansionResult (avoids a redundant evaluate_rules call for website scope)."""
    rules = [r for r in ALL_RULES if r.scope == scope]
    if not rules:
        st.info(f"No rules defined for scope {label!r}.")
        return
    if result is None:
        result = evaluate_rules(tuple(r.name for r in rules))
    raw = result.raw_cases
    automated = result.automated

    c1, c2, c3 = st.columns(3)
    non_dep = raw[raw["deprecated"] == False] if not raw.empty else raw  # noqa: E712
    auto_ids = set(automated["case_id"].unique()) if not automated.empty else set()
    auto_n = int(non_dep["case_id"].isin(auto_ids).sum()) if not non_dep.empty else 0
    total = int(len(non_dep))
    c1.metric("Total cases", f"{total:,}")
    c2.metric("Automated", f"{auto_n:,}")
    c3.metric("Coverage", f"{(auto_n / total * 100 if total else 0):.1f}%")

    level = st.slider("Section depth", 1, 4, 1,
                      key=f"cov_depth_{scope}",
                      help="How deep to walk the section hierarchy for grouping.")
    cov = metrics.coverage_by_section(raw, automated, section_level=level)
    if cov.empty:
        st.info("No sections to display.")
        return
    cov_disp = cov.copy()
    cov_disp["coverage"] = (cov_disp["coverage"] * 100).round(1)
    st.dataframe(
        cov_disp,
        use_container_width=True,
        hide_index=True,
        column_config={
            "section": st.column_config.TextColumn("Section", width="large"),
            "total": st.column_config.NumberColumn("Total"),
            "automated": st.column_config.NumberColumn("Automated"),
            "coverage": st.column_config.ProgressColumn(
                "Coverage %", min_value=0, max_value=100, format="%.1f%%"),
        },
    )

    # Scope-specific facets
    if scope == "mobile_app" and not automated.empty and "automation_tool" in automated.columns:
        st.markdown("##### Automated cases by automation tool")
        tool = (automated.dropna(subset=["automation_tool"])
                .drop_duplicates(subset=["bu", "case_id"])
                .groupby(["bu", "automation_tool"]).size()
                .reset_index(name="count"))
        if not tool.empty:
            st.dataframe(tool, use_container_width=True, hide_index=True)
        else:
            st.caption("No `Automation Tool` values populated on matching cases.")

    if scope in ("website", "next_gen") and not automated.empty:
        st.markdown("##### Automated cases by framework (legacy Java vs TestIM)")
        fw_label = automated["framework"].map({
            "java": "Legacy (Java + Selenide + Cucumber)",
            "testim_desktop": "TestIM Desktop",
            "testim_mobile": "TestIM Mobile",
        }).fillna(automated["framework"])
        tmp = automated.assign(framework_label=fw_label)
        pivot = (tmp.drop_duplicates(subset=["bu", "country_label", "case_id", "framework_label"])
                 .groupby(["bu", "framework_label"]).size()
                 .reset_index(name="count"))
        st.dataframe(pivot, use_container_width=True, hide_index=True)


# --------------------------------------------------------------------- render
def render() -> None:
    st.subheader("🧭 Automation Coverage Overview")

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
            _metric_card("Smoke (Highest automated)", smoke, "#ff6b35")
        with c2:
            _metric_card("No-Regression (All automated)", regr, "#2e5bff")
        with c3:
            _metric_card("Production Sanity", sanity, "#1dbf73")

    st.divider()
    st.markdown("### Coverage by section")
    tw, tm, tn = st.tabs(["🌐 Website", "📱 Mobile App", "🧩 Next Gen"])
    with tw:
        # Reuse the already-computed website result — no extra API calls
        _coverage_view("website", "Website", result=result)
    with tm:
        _coverage_view("mobile_app", "Mobile App")
    with tn:
        _coverage_view("next_gen", "Next Gen")
