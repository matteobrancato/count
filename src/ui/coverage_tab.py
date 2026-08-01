"""Coverage tab — automation coverage per functional area (TestRail section).

Output mirrors the manual "coverage_outputs_<BU>.xlsx" Chiara produces:
  * Section names normalised by auto-stripping dominant "container roots"
    (e.g. "SD" or "WTR > Root") so the rows match the Excel "Main Category".
  * Desktop / Mobile / Unspecified columns count EXPANDED rows (same convention
    as the Report tab) — a case automated for both devices counts twice.
  * Coverage % on the baseline view divides EXPANDED ROWS, reusing the Backlog
    tab's own classified frame — so both tabs report one number for a BU, by
    construction rather than by coincidence (locked by tests/test_business_rules
    ::TestCoverageAgreesWithBacklog).  The Total / Production Sanity views have
    no row expansion and stay case-based, labelled "Coverage by Case".

Three stacked views per BU
──────────────────────────
  1. **All Automated Cases** — coverage over the full non-deprecated universe.
  2. **No-Regression Baseline Only** — restricted to cases tagged with
     `big_regr_desktop` / `big_regr_mobile` (the regression baseline used by the
     Backlog tab), with device-specific label matching.
  3. **Production Sanity Only** — restricted to cases flagged for production
     sanity — the `prod_sanity` LABEL, the same one the Backlog tab's
     Production Sanity baseline uses.  Same convention as Overview.

All three views share the same renderer (`_render_coverage_section`) so the
layout is identical — only the input subset changes.

Layout per view
───────────────
  * Headline metrics: Total · Automated · Backlog · Coverage
  * Granularity slider (0 = Main Category, 1 = Secondary, 2-3 = deeper)
  * Table — Area | Total | Desktop | Mobile [| Unspecified] | Automated | Coverage %
  * Pie chart — share of automated rows per area
  * Bar chart — coverage % per area (sorted, zero rows pushed to the bottom)
"""
from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from ..bu_rules import ALL_RULES, filter_conditional_tokens
from .. import testrail_client as tr
from ..rules_engine import evaluate_rules
from . import global_filter
from .styles import COLORS, COVERAGE_TARGET, PIE_PALETTE, section_title

# ── categorical palette for area breakdowns (sourced from design tokens) ──────
# Repeated so very granular BUs (>12 areas) still get a colour for every slice.
_PIE_PALETTE = PIE_PALETTE * 2


# ── data loading ─────────────────────────────────────────────────────────────
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
        # `top=top` binds the loop variable at definition time — without it the
        # lambda would close over the *latest* `top` if evaluation were ever
        # deferred (it isn't today, but this keeps it correct by construction).
        current = current.map(
            lambda p, top=top: p[1:] if (p and p[0] == top) else None
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
    expanded: pd.DataFrame | None = None,
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

    keep = ["case_id", "section_path"] + [c for c in ("section_id", "suite_id")
                                          if c in non_dep.columns]
    work = non_dep[keep].copy()
    work["section"] = work["section_path"].fillna("").map(
        lambda p: _section_for_path(p, chain, depth_offset)
    )
    work["_is_auto"] = work["case_id"].isin(auto_ids)

    aggs = {"total": ("case_id", "nunique"), "auto_unique": ("_is_auto", "sum")}
    # A row of this table can span SEVERAL TestRail sections (the label is the
    # path truncated at the chosen depth).  Carrying the section count lets us
    # link only the rows that map to exactly one — a link that silently landed
    # on one of five sections would be worse than no link.
    if "section_id" in work.columns:
        aggs |= {"_n_sections": ("section_id", "nunique"),
                 "_section_id": ("section_id", "first")}
    if "suite_id" in work.columns:
        aggs["_suite_id"] = ("suite_id", "first")

    grouped = work.groupby("section", dropna=False).agg(**aggs).reset_index()
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
                                  ("Mobile",  mobile_map)]:
            if dev_name in dev_grp.columns:
                target.update(dev_grp[dev_name].to_dict())
        # "Unspecified" is a catch-all for any non-Desktop/Mobile device —
        # including Microservices's "API" — so the automated total stays correct
        # (automated = desktop + mobile + unspecified).
        other_cols = [c for c in dev_grp.columns if c not in ("Desktop", "Mobile")]
        if other_cols:
            unspecified_map.update(dev_grp[other_cols].sum(axis=1).to_dict())

    grouped["desktop"]     = grouped["section"].map(desktop_map).fillna(0).astype(int)
    grouped["mobile"]      = grouped["section"].map(mobile_map).fillna(0).astype(int)
    grouped["unspecified"] = grouped["section"].map(unspecified_map).fillna(0).astype(int)
    grouped["automated"]   = (
        grouped["desktop"] + grouped["mobile"] + grouped["unspecified"]
    )

    # Denominator.  With *expanded* (the Backlog tab's classified baseline rows)
    # every column is on the SAME basis — expanded rows — so Coverage % here is
    # the number the Backlog tab and the KPI strip show.  Without it (Total /
    # Production Sanity views, which have no row expansion) the table stays on
    # the unique-case basis it has always used.
    if expanded is not None and not expanded.empty:
        exp = expanded[["section_path"]].copy()
        exp["section"] = exp["section_path"].fillna("").map(
            lambda p: _section_for_path(p, chain, depth_offset)
        )
        rows_map = exp.groupby("section").size().to_dict()
        grouped["total"] = (
            grouped["section"].map(rows_map).fillna(0).astype(int)
        )
        numerator = grouped["automated"]
    else:
        numerator = grouped["auto_unique"]

    grouped["coverage_pct"] = (
        (numerator / grouped["total"].replace(0, pd.NA) * 100)
        .astype(float).round(1).fillna(0.0)
    )

    # Sort: non-zero automated first (by automated desc), then zero rows at the bottom
    # (by total desc, so the biggest "empty" areas float to the top of the zero block).
    grouped["_zero_flag"] = (grouped["automated"] == 0).astype(int)
    grouped = grouped.sort_values(
        by=["_zero_flag", "automated", "total"],
        ascending=[True, False, False],
    ).drop(columns=["_zero_flag"]).reset_index(drop=True)

    # ── Readable label + a link to the section in TestRail ──────────────────
    # At depth > 0 every label repeats its parent ("Content Management > [DRG]
    # Google A…"), so the axis showed the shared prefix and truncated the part
    # that tells them apart.  The chart uses the LEAF; the full path stays in
    # the tooltip and in the table.
    grouped["area"] = grouped["section"].map(
        lambda v: v.split(" > ")[-1].strip() if isinstance(v, str) else v)
    grouped["section_url"] = _section_urls(grouped)

    cols = ["section", "area", "section_url", "total", "desktop", "mobile",
            "unspecified", "automated", "auto_unique", "coverage_pct"]
    return grouped[[c for c in cols if c in grouped.columns]], chain


def _section_urls(grouped: pd.DataFrame) -> pd.Series:
    """A TestRail link per row — only where the row IS one section.

    TestRail can open a suite grouped and scrolled to a section, which is the
    closest thing to "show me these cases".  Rows that aggregate several
    sections get no link rather than a link to an arbitrary one of them.
    """
    blank = pd.Series([""] * len(grouped), index=grouped.index)
    if "_section_id" not in grouped.columns or "_suite_id" not in grouped.columns:
        return blank
    try:
        base = tr.TestRailCredentials.from_secrets().base_url.rstrip("/")
    except Exception:                                                   # noqa: BLE001
        return blank
    single = grouped.get("_n_sections", pd.Series(1, index=grouped.index)) == 1
    urls = (base + "/index.php?/suites/view/"
            + grouped["_suite_id"].astype("Int64").astype(str)
            + "&group_by=cases:section_id&group_order=asc&group_id="
            + grouped["_section_id"].astype("Int64").astype(str))
    return urls.where(single & grouped["_suite_id"].notna()
                      & grouped["_section_id"].notna(), "")


# ── charts ───────────────────────────────────────────────────────────────────
def _panel_head(title: str, caption: str) -> None:
    """Heading + caption for a chart panel, at a FIXED height.

    The two panels sit in side-by-side columns, so a caption that wrapped to two
    lines on one side pushed that chart 22px below its neighbour.  One block of
    a fixed height makes both charts start on exactly the same line, whatever
    the column width.  Inline <span>s (not <div>s) so the block keeps its full
    height inside Streamlit's markdown container — see the note in app.py.
    """
    st.markdown(
        f'<span class="cov-panel-head">'
        f'<span class="cov-panel-title">{title}</span>'
        f'<span class="cov-panel-sub">{caption}</span>'
        f'</span>',
        unsafe_allow_html=True,
    )


def _area_color_map(cov: pd.DataFrame) -> dict[str, str]:
    """Stable area → palette-color mapping, shared between pie and bar charts.

    Areas are ordered by automated DESC first (so the biggest slice gets the
    first palette color, then the second-biggest gets the second, etc.).  This
    keeps colors consistent across both charts even though the bar chart sorts
    by coverage %.
    """
    ordered = cov.sort_values("automated", ascending=False)["section"].tolist()
    return {area: _PIE_PALETTE[i % len(_PIE_PALETTE)] for i, area in enumerate(ordered)}


# Beyond this many slices the palette would repeat (breaking "same colour =
# same area") and thin slices become unreadable — the tail goes into "Other".
_PIE_MAX_SLICES = 11
_PIE_OTHER_COLOR = COLORS["faint"]


def _build_pie(cov: pd.DataFrame, color_map: dict[str, str]) -> alt.Chart | None:
    """Pie of automated case distribution across sections (slice size = automated).

    The top areas keep their palette colour; anything beyond _PIE_MAX_SLICES is
    bucketed into a single grey "Other" slice."""
    data = cov[cov["automated"] > 0].copy()
    if data.empty:
        return None
    if len(data) > _PIE_MAX_SLICES:
        data = data.sort_values("automated", ascending=False).reset_index(drop=True)
        head, tail = data.iloc[:_PIE_MAX_SLICES], data.iloc[_PIE_MAX_SLICES:]
        n_tail = len(tail)
        other = pd.DataFrame([{
            "section":      f"Other ({n_tail} areas)",
            "total":        int(tail["total"].sum()),
            "desktop":      int(tail["desktop"].sum()),
            "mobile":       int(tail["mobile"].sum()),
            "unspecified":  int(tail["unspecified"].sum()),
            "automated":    int(tail["automated"].sum()),
            "auto_unique":  int(tail["auto_unique"].sum()),
            "coverage_pct": round(float(tail["auto_unique"].sum())
                                  / float(tail["total"].sum()) * 100, 1)
                            if tail["total"].sum() else 0.0,
        }])
        data = pd.concat([head, other], ignore_index=True)
        color_map = {**color_map, f"Other ({n_tail} areas)": _PIE_OTHER_COLOR}
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
    # Share of each slice — used to label only the slices big enough to read.
    grand = float(data["automated"].sum()) or 1.0
    data = data.assign(
        _share=data["automated"] / grand * 100,
        _lbl=lambda d: [
            f"{p:.0f}%" if p >= 7 else ""            # tiny slices stay unlabelled
            for p in (d["automated"] / grand * 100)
        ],
    )
    base = base.properties(data=data)

    # Bigger ring than before: the labels moved inside, so the chart no longer
    # has to reserve a band of empty space around it for outer text.
    arc = base.mark_arc(innerRadius=62, outerRadius=124, cornerRadius=3,
                        stroke=COLORS["canvas"], strokeWidth=3)
    # Percentages INSIDE the ring.  Area names used to sit outside it, but the
    # panel is only ~550px wide, so every one of them was cut off mid-word
    # ("Product Information Ma…").  The names live in the bar chart to the
    # right, which shares this chart's colours — hence the caption.
    labels = base.mark_text(radius=93, fontSize=11.5, fontWeight="bold",
                            font="Inter").encode(
        text=alt.Text("_lbl:N"), color=alt.value("#FFFFFF"),
    )
    # The hole is free real estate: put the total there.
    centre = alt.Chart(pd.DataFrame([{"t": f"{int(grand):,}"}])).mark_text(
        fontSize=19, fontWeight="bold", font="Inter", fill=COLORS["ink"], dy=-6,
    ).encode(text="t:N")
    centre_sub = alt.Chart(pd.DataFrame([{"t": "automated rows"}])).mark_text(
        fontSize=9.5, font="Inter", fill=COLORS["muted"], dy=11,
    ).encode(text="t:N")

    return (arc + labels + centre + centre_sub).properties(
        height=280, background="transparent")


def _build_coverage_bar(cov: pd.DataFrame, color_map: dict[str, str]) -> alt.Chart:
    """Horizontal bars: coverage % per section.

    Sort: zero-automated rows pushed to the bottom (same convention as the table).
    Colors: same per-area palette as the pie chart, so a colour means the same
    area in both views.
    """
    data = cov.copy()
    if "section_url" not in data.columns:
        data["section_url"] = ""
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
    y_order = data.sort_values("_sort_key")["section"].tolist()

    # Axis label = the LEAF of the path.  At depth > 0 every label shared the
    # same parent prefix, so truncation ate the only distinguishing part.  It
    # is computed HERE rather than with an axis `labelExpr`, which silently
    # collapses the chart to zero height when the height is step-based.
    # Two areas can share a leaf name ("[DRG] Cart" under two parents), so an
    # ambiguous leaf falls back to its full path — the y field must stay unique
    # or the two would merge into one bar.
    if "area" not in data.columns:
        data["area"] = data["section"].map(
            lambda v: v.split(" > ")[-1].strip() if isinstance(v, str) else v)
    dupes = data["area"].value_counts()
    data["_label"] = [a if dupes.get(a, 0) == 1 else s
                      for a, s in zip(data["area"], data["section"])]
    label_of = dict(zip(data["section"], data["_label"]))
    y_labels = [label_of[s] for s in y_order]

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
                                  gridColor=COLORS["grid"], domain=False,
                                  labelColor=COLORS["muted"], titleColor=COLORS["muted"])),
            # The axis shows the LEAF ("[DRG] Google Analytics"), not the whole
            # path — at depth > 0 every label shared the same parent prefix, so
            # the truncation ate the only part that told them apart.  Sorting
            # still keys on the full path, which is unique.
            y=alt.Y("_label:N", sort=y_labels,
                    axis=alt.Axis(title=None, labelLimit=260,
                                  domain=False, ticks=False,
                                  labelColor=COLORS["text"])),
            color=alt.Color("section:N", scale=color_scale, legend=None),
            # NOTE: no `href` encoding here.  The data carries the section URL
            # and Vega-Lite accepts the channel, but Streamlit's chart embed
            # renders no <a> elements for it (verified: 98 marks, 0 links), so
            # the bars would look clickable and do nothing.  The link lives in
            # the Coverage Table below, where Streamlit's LinkColumn is native.
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
        .mark_text(align="left", dx=5, fontSize=10, color=COLORS["muted"])
        .encode(
            x=alt.X("coverage_pct:Q"),
            y=alt.Y("_label:N", sort=y_labels),
            text=alt.Text("label:N"),
        )
    )

    return (
        alt.layer(bars, text)
        .properties(height=alt.Step(26), background="transparent")
        .configure_view(stroke="transparent", strokeWidth=0, fill="transparent")
        .configure_axis(labelFont="Inter")
    )


# ── regression-baseline filter ───────────────────────────────────────────────
def _filter_to_bu_countries(
    non_dep: pd.DataFrame, rules_bu: list,
) -> tuple[pd.DataFrame, int]:
    """Keep only cases whose country field carries one of the BU's tokens —
    the same convention the automated set uses, so the two agree.

    On suites shared between BUs (Eastern Europe, Kruidvat/Trekpleister,
    Superdrug/Savers) this removes the other BUs' cases from the denominator,
    so Coverage totals match the automated set instead of counting the whole suite for
    every BU.  Returns (filtered, n_excluded); BUs without country filters are
    passed through untouched."""
    country_col = "multi_countries"
    for r in rules_bu:
        if getattr(r, "country_field_label", "multi_countries") == "custom_country_coverage":
            country_col = "country_coverage"
            break
    all_tokens: set[str] = set()
    for r in rules_bu:
        all_tokens.update(r.countries_filter or [])
    if not all_tokens or non_dep.empty or country_col not in non_dep.columns:
        return non_dep, 0
    prios = (non_dep["priority_label"] if "priority_label" in non_dep.columns
             else pd.Series([None] * len(non_dep), index=non_dep.index))
    has_tok = pd.Series(
        [any(t in all_tokens for t in filter_conditional_tokens(
            mc if isinstance(mc, list) else [], prio))
         for mc, prio in zip(non_dep[country_col], prios)],
        index=non_dep.index,
    )
    return non_dep[has_tok], int((~has_tok).sum())


def _regression_baseline_like_backlog(
    non_dep: pd.DataFrame, auto_bu: pd.DataFrame, rules_bu: list,
) -> tuple[pd.DataFrame, pd.DataFrame, set[int], pd.DataFrame]:
    """Regression baseline computed EXACTLY like the Backlog tab.

    Reuses the Backlog's own expansion (`_expand_baseline` + `_classify_expanded`):
    each big_regr case is expanded over its `multi_countries` countries × the
    label-driven device, then classified against the automated set.  This keeps
    the Coverage "No-Regression Baseline Only" view aligned 1:1 with the Backlog
    tab — the previous filter used the framework Country-Coverage expansion, which
    counted automated rows for countries NOT present in `multi_countries`.

    Returns (non_dep_baseline, automated_rows, baseline_auto_case_ids, expanded)
    — `expanded` is the FULL classified frame (every category, with
    `section_path` attached), i.e. the very rows the Backlog tab counts.  It is
    what makes Coverage's headline numbers identical to the Backlog's instead of
    merely "computed the same way": same rows, same denominator.
    """
    from . import backlog_tab as bl

    empty = (non_dep.iloc[0:0], auto_bu.iloc[0:0], set(), pd.DataFrame())
    if non_dep.empty:
        return empty
    # Mobile App has no big_regr baseline — it uses the priority-based MAPP
    # baseline (High/Highest × iOS/Android).  Dispatch to the right expansion so
    # the Coverage "baseline" view matches the Backlog tab for every scope.
    _expand = (bl._expand_mapp_baseline
               if rules_bu and rules_bu[0].scope == "mobile_app"
               else bl._expand_baseline)
    expanded = bl._classify_expanded(_expand(non_dep, rules_bu), auto_bu)
    if expanded.empty:
        return empty

    expanded = expanded.copy()
    expanded["case_id"] = expanded["case_id"].astype(int)
    # Attach per-case section_path so every row can be broken down by area.
    sec = non_dep[["case_id", "section_path"]].copy()
    sec["case_id"] = sec["case_id"].astype(int)
    expanded = expanded.merge(sec.drop_duplicates("case_id"), on="case_id", how="left")

    base_ids = set(expanded["case_id"].unique())
    nd_base  = non_dep[non_dep["case_id"].astype(int).isin(base_ids)]
    auto_rows = expanded[expanded["category"] == "automated"].copy()

    return nd_base, auto_rows, set(auto_rows["case_id"].unique()), expanded


def _filter_to_prod_sanity(
    non_dep: pd.DataFrame, auto_bu: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, set[int]]:
    """Filter both DataFrames to Production Sanity cases — tests executed only in
    production (the `prod_sanity` label → `is_prod_sanity` flag).  Same
    convention as the Overview tab's "Production Sanity" card.

    Returns (non_dep_prod_sanity, auto_bu_prod_sanity, prod_sanity_auto_case_ids).
    """
    if non_dep.empty or "prod_sanity" not in non_dep.columns:
        return non_dep.iloc[0:0], auto_bu.iloc[0:0], set()

    nd_ps = non_dep[non_dep["prod_sanity"] == True]  # noqa: E712
    if nd_ps.empty or auto_bu.empty or "is_prod_sanity" not in auto_bu.columns:
        return nd_ps, auto_bu.iloc[0:0], set()

    ab_ps = auto_bu[auto_bu["is_prod_sanity"] == True]  # noqa: E712
    return nd_ps, ab_ps, set(ab_ps["case_id"].astype(int).unique())


# ── per-BU view ──────────────────────────────────────────────────────────────
def _render_coverage_section(
    non_dep: pd.DataFrame,
    auto_bu: pd.DataFrame,
    auto_ids: set[int],
    *,
    scope: str,
    depth_offset: int = 0,
    show_tool_facet: bool = True,
    show_target: bool = False,
    expanded: pd.DataFrame | None = None,
) -> None:
    """Render the full coverage block (metrics + table + charts) for a subset.

    Pulled out of `_coverage_for` so all three views (Total / baseline / prod
    sanity) share one layout — only the input subset changes.  The function
    renders no widgets of its own (the view radio and granularity slider live in
    `_coverage_for`), so it needs no per-call widget key.

    *show_target* adds a vs-target delta inside the Coverage metric — enabled
    only on the regression-baseline view, where the 80% target applies ("coverage"
    targets are defined on the baseline, not on the whole case universe).
    """
    if non_dep.empty:
        st.info("No cases in this subset.")
        return

    # ── headline metrics ──────────────────────────────────────────────────────
    # ONE basis per view, and where the Backlog tab also measures this BU it is
    # the Backlog's basis: expanded rows (case × country × device).  Coverage
    # used to divide unique CASES here while the Backlog divided ROWS, so the
    # same BU read 91.9% on one tab and 95.2% on the other.  Same rows now.
    auto_unique = int(non_dep["case_id"].isin(auto_ids).sum())
    cases_total = int(non_dep["case_id"].nunique())
    by_row      = expanded is not None and not expanded.empty

    c1, c2, c3, c4 = st.columns(4)
    if by_row:
        # Same four words, same four numbers as the Backlog tab — a manager
        # comparing the two tabs should never have to translate a label.
        total     = int(len(expanded))
        auto_rows = int((expanded["category"] == "automated").sum())
        backlog   = int((expanded["category"] == "backlog").sum())
        partial   = int((expanded["category"] == "partially_automated").sum())
        cov_pct   = (auto_rows / total * 100) if total else 0.0
        c1.metric("Total", f"{total:,}",
                  help=f"Baseline rows: case × country × device — the same rows "
                       f"the Backlog tab counts. {cases_total:,} unique cases.")
        c2.metric("Automated", f"{auto_rows:,}",
                  help=f"{auto_unique:,} unique cases.")
        c3.metric("Backlog", f"{backlog:,}",
                  help=f"Not automated in ANY country or device — the same "
                       f"figure as the Backlog tab. A further {partial:,} rows "
                       f"belong to cases automated elsewhere (Partially "
                       f"Automated there).")
        _cov_help = ("Automated rows ÷ baseline rows — the SAME basis as the "
                     "Backlog tab and the KPI strip, so all three agree.")
    else:
        # Total / Production Sanity have no baseline row expansion, so they stay
        # on the unique-case basis — labelled, not silently mixed.
        total     = cases_total
        cov_pct   = (auto_unique / total * 100) if total else 0.0
        c1.metric("Total Cases", f"{total:,}")
        c2.metric("Automated Cases", f"{auto_unique:,}")
        c3.metric("Automated Rows", f"{len(auto_bu):,}",
                  help="Expanded rows: Desktop + Mobile. Same convention as "
                       "the Report tab.")
        _cov_help = ("Automated cases ÷ total cases.  This view has no baseline "
                     "row expansion (that is defined on the regression baseline "
                     "only), so it counts cases — hence the label.")

    _cov_label = "Coverage" if by_row else "Coverage by Case"
    if show_target:
        c4.metric(_cov_label, f"{cov_pct:.1f}%",
                  delta=f"{cov_pct - COVERAGE_TARGET:+.1f}% vs {COVERAGE_TARGET:.0f}% target",
                  delta_color="normal", help=_cov_help)
    else:
        c4.metric(_cov_label, f"{cov_pct:.1f}%", help=_cov_help)

    # depth_offset (granularity) now comes from the control row in _coverage_for
    # so the picker can sit next to the view radio.
    cov, chain = _coverage_table(non_dep, auto_bu, auto_ids,
                                 depth_offset=depth_offset, expanded=expanded)
    if cov.empty:
        st.info("No sections to display.")
        return

    # ── charts FIRST — the at-a-glance visual managers care about ─────────────
    # Build one color map shared by both charts so a given area is always the
    # same color across pie + bar.
    color_map = _area_color_map(cov)
    st.markdown("")
    left, right = st.columns([1, 1.2], gap="large")
    with left:
        _panel_head("Automated Distribution", "")
        pie = _build_pie(cov, color_map)
        if pie is None:
            st.info("No automated cases yet.")
        else:
            st.altair_chart(pie, width="stretch")
    with right:
        _panel_head("Coverage % per Area",
                    "Sorted by coverage %. Same colour = same area as the pie.")
        bar = _build_coverage_bar(cov, color_map)
        st.altair_chart(bar, width="stretch")

    # ── table (detail, below the charts) ──────────────────────────────────────
    section_title("Coverage Table")
    # (The auto-stripped container chain used to be spelled out here; it is
    # TestRail-folder trivia, not something a manager acts on.)
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
        "section_url":  "",          # the Total row is not a TestRail section
    }])
    display = pd.concat([display, total_row], ignore_index=True)

    # Only show Unspecified column if any value is non-zero (typically Microservices)
    show_unspecified = bool(display["unspecified"].sum() > 0)
    cols = ["section", "total", "desktop", "mobile"]
    if show_unspecified:
        cols.append("unspecified")
    cols += ["automated", "coverage_pct"]
    # Link column only when there is something to link — a column of empty
    # cells would just take width away from the numbers.
    has_links = ("section_url" in display.columns
                 and display["section_url"].astype(str).str.len().gt(0).any())
    if has_links:
        cols.append("section_url")

    auto_label = "Automated"

    st.dataframe(
        display[cols],
        width="stretch",
        hide_index=True,
        column_config={
            "section":      st.column_config.TextColumn(
                "Main Category" if depth_offset == 0
                else ("Secondary Category" if depth_offset == 1
                      else f"Area (depth +{depth_offset})"),
                width="large"),
            "total":        st.column_config.NumberColumn(
                "Total",
                help=("Baseline rows (case × country × device) in this area — "
                      "the Backlog tab's basis." if by_row
                      else "Unique non-deprecated cases in this area.")),
            "desktop":      st.column_config.NumberColumn("Desktop"),
            "mobile":       st.column_config.NumberColumn("Mobile"),
            "unspecified":  st.column_config.NumberColumn("Unspecified"),
            "automated":    st.column_config.NumberColumn(
                auto_label,
                help="Sum of expanded rows per device.  Matches the Excel \"Total\" column."),
            "section_url":  st.column_config.LinkColumn(
                "TestRail", width="small", display_text="open ↗",
                help="Opens the section in TestRail.  Only rows that ARE one "
                     "section get a link: at a coarse granularity a row folds "
                     "several sections together, and the link would land on an "
                     "arbitrary one of them.  Raise the granularity for more "
                     "links."),
            "coverage_pct": st.column_config.ProgressColumn(
                "Coverage %", format="%.1f%%", min_value=0, max_value=100,
                help=("Automated rows ÷ baseline rows per area — the Backlog "
                      "tab's basis." if by_row
                      else "Automated cases ÷ total cases per area.")),
        },
    )

    # ── mobile-app facet (only shown in the full view to avoid duplication) ──
    if show_tool_facet and scope == "mobile_app" and not auto_bu.empty \
            and "automation_tool" in auto_bu.columns:
        st.divider()
        section_title("Automated Cases by Automation Tool")
        tool = (
            auto_bu.dropna(subset=["automation_tool"])
            .drop_duplicates(subset=["case_id"])
            .groupby("automation_tool").size()
            .reset_index(name="count")
        )
        if not tool.empty:
            st.dataframe(tool, width="stretch", hide_index=True)
        else:
            st.caption("No `Automation Tool` values populated on matching cases.")


# ── the three coverage subsets, selected by one radio (default: the baseline) ─
# "No-Regression" is the internal name of the WEBSITE regression baseline, so it
# is kept for website/microservices; Mobile App's baseline is priority-based and
# has nothing to do with regression, hence the neutral label there.
_VIEW_TOTAL = "🌐 Total"
_VIEW_REGR  = "📋 No-Regression"
_VIEW_REGR_MAPP = "📋 Baseline"
_VIEW_PS    = "🚀 Production Sanity"
_VIEW_OPTIONS = [_VIEW_TOTAL, _VIEW_REGR, _VIEW_PS]
_VIEW_OPTIONS_MAPP = [_VIEW_TOTAL, _VIEW_REGR_MAPP, _VIEW_PS]
_VIEW_DEFAULT_INDEX = 1                                  # the baseline view

# Section-depth picker: named levels beat raw 0-3 on a slider.
_GRAN_LEVELS: list[int] = [0, 1, 2, 3]
_GRAN_LABELS: dict[int, str] = {0: "Main", 1: "Sub", 2: "+2", 3: "+3"}
_GRAN_HELP = ("Section depth: Main = top-level category (auto-detected, strips "
              "dominant root folders like SD or WTR); Sub = secondary; "
              "+2 / +3 = deeper sub-sections.")


def _coverage_for(scope: str, bu_choice: str) -> None:
    # Mobile App is not pre-warmed (deferred from the start-up download): the
    # first visit fetches its 7 suites live — show an honest spinner for that
    # one-time wait.  Warm visits fall through instantly.
    if scope == "mobile_app":
        with st.spinner("📱 Loading Mobile App data — first time can take "
                        "~30-60s, then it's cached…"):
            raw, auto, rules = _load_scope(scope)
    else:
        raw, auto, rules = _load_scope(scope)
    if raw is None or raw.empty:
        st.info("No data loaded for this scope.")
        return

    # `rules` is already filtered to *scope* by _load_scope, so no second scope check.
    rules_bu  = [r for r in rules if r.bu == bu_choice]
    bu_suites = {r.suite_id for r in rules_bu}

    raw_bu  = raw[raw["suite_id"].isin(bu_suites)]
    auto_bu = auto[auto["bu"] == bu_choice] if not auto.empty else auto
    # Dedup dual-framework rows on (case, country, device) — the same
    # convention the Report tab uses.  Without this a case automated by
    # BOTH Java and Testim would count as two D+M rows here but one there.
    if not auto_bu.empty:
        auto_bu = auto_bu.drop_duplicates(subset=["case_id", "country_label", "device"])

    if raw_bu.empty:
        st.info(f"No cases found for **{bu_choice}**.")
        return

    non_dep  = raw_bu[raw_bu["deprecated"] == False]  # noqa: E712
    non_dep, n_other_bu = _filter_to_bu_countries(non_dep, rules_bu)
    auto_ids = set(auto_bu["case_id"].unique()) if not auto_bu.empty else set()

    # ── ONE view + granularity on ONE control row ─────────────────────────────
    # View radio (left) and the granularity picker (right) share a line so the
    # metrics sit high on the page.  The picker is per-(scope, BU) so it persists
    # when switching views.  The shared-suite exclusion note rides along the
    # per-view description caption below (kept compact).
    #
    # Granularity used to be a slider: three stacked lines (label / value /
    # track) with the help icon flung to the far right of the label row, in a
    # block 28px taller than the radio it sits next to.  A segmented control is
    # one line, the same height as the radio, and names the depths instead of
    # asking the reader to decode 0-3.
    is_mapp = scope == "mobile_app"
    options = _VIEW_OPTIONS_MAPP if is_mapp else _VIEW_OPTIONS
    c_radio, c_gran = st.columns([3, 2], vertical_alignment="center")
    with c_radio:
        view = st.radio(
            "Coverage view", options, index=_VIEW_DEFAULT_INDEX,
            horizontal=True, key=f"cov_view_{scope}_{bu_choice}",
            label_visibility="collapsed",
        )
    with c_gran, st.container(
        key="cov_gran_row", horizontal=True, vertical_alignment="center",
        horizontal_alignment="right", gap="small",
    ):
        st.markdown(
            f"<span title='{_GRAN_HELP}' style='font-size:13px;"
            f"color:{COLORS['muted']};white-space:nowrap;cursor:help'>"
            f"Granularity</span>",
            unsafe_allow_html=True,
        )
        depth_offset = st.segmented_control(
            "Granularity", _GRAN_LEVELS, default=0, required=True,
            format_func=lambda v: _GRAN_LABELS[v],
            key=f"cov_gran_seg_{scope}_{bu_choice}",
            label_visibility="collapsed",
        )
        depth_offset = int(depth_offset if depth_offset is not None else 0)
    is_baseline_view = view in (_VIEW_REGR, _VIEW_REGR_MAPP)

    if view == _VIEW_TOTAL:
        _render_coverage_section(
            non_dep, auto_bu, auto_ids,
            scope=scope, depth_offset=depth_offset, show_tool_facet=True,
        )
    elif is_baseline_view:
        nd_base, ab_base, ids_base, exp_base = _regression_baseline_like_backlog(
            non_dep, auto_bu, rules_bu)
        if nd_base.empty:
            st.info(
                "No cases tagged with `big_regr_desktop` / `big_regr_mobile` for "
                "this BU. Add the labels in TestRail — they appear at the next "
                "data refresh (↻ next to the tabs)."
            )
        else:
            _render_coverage_section(
                nd_base, ab_base, ids_base,
                scope=scope, depth_offset=depth_offset, show_tool_facet=True,
                show_target=True,        # the 80% target is defined on the baseline
                expanded=exp_base,       # → same rows (and %) as the Backlog tab
            )
    else:  # _VIEW_PS
        nd_ps, ab_ps, ids_ps = _filter_to_prod_sanity(non_dep, auto_bu)
        if nd_ps.empty:
            st.info(
                "No Production Sanity cases found for this BU. Mark cases with "
                "the `Test Automation PRD Run` checkbox in TestRail — new flags "
                "appear at the next data refresh (↻ next to the tabs)."
            )
        else:
            _render_coverage_section(
                nd_ps, ab_ps, ids_ps,
                scope=scope, depth_offset=depth_offset, show_tool_facet=True,
            )


# ── render ───────────────────────────────────────────────────────────────────
@st.fragment
def render() -> None:
    # No lead caption / subheader: the tab is already labeled "Coverage" and the
    # per-view description below the control row states the counting convention.

    # Scope + BU come from the GLOBAL control bar (global_filter) — no local
    # selectors, one standardized method across every tab.
    chosen_scope, bu_choice = global_filter.current()
    if not bu_choice:
        st.info("No rules defined for this scope.")
        return

    # Keyed wrapper = scope hook for the fade-in animation (styles.py:
    # `.st-key-coverage_anim`).  No divider above: it left a big empty gap
    # before the view controls.
    with st.container(key="coverage_anim"):
        _coverage_for(chosen_scope, bu_choice)
