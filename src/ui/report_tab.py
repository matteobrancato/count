from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from .. import metrics
from ..bu_rules import ALL_RULES
from ..rules_engine import evaluate_rules
from . import global_filter
from .styles import (
    COLORS,
    COVERAGE_TARGET,
    coverage_health,
    section_title,
    stat_card,
)


def _scope_summary(scope: str) -> pd.DataFrame:
    """Per-BU baseline summary for the leaderboard, filtered to *scope* — the
    same source the Backlog tab and KPI strip use, so numbers reconcile."""
    from . import backlog_tab as bl
    try:
        if scope == "mobile_app":
            summary, _, _ = bl._mapp_backlog_data()
            return summary
        summary, _, _ = bl._backlog_data()
        want = {"next_gen": "Microservices"}.get(scope, "Website")
        return summary[summary["Scope"] == want]
    except Exception:                                                   # noqa: BLE001
        return pd.DataFrame()

# ── palette (sourced from the global design tokens) ─────────────────────────────
_BLUE    = COLORS["mobile"]       # Mobile
_ORANGE  = COLORS["desktop"]      # Desktop
_GREY    = COLORS["unspecified"]  # Unspecified (Microservices)
_IOS     = "#6366F1"              # iOS     (Mobile App)
_ANDROID = "#16A34A"             # Android (Mobile App)

_BU_ORDER = [
    "The Perfume Shop", "Savers", "Superdrug",
    "Kruidvat", "Trekpleister", "Watsons", "Drogas",
    "Marionnaud", "ICI Paris XL", "Microservices",
]


# ── data loading (shares evaluate_rules cache with other tabs) ────────────────
def _add_regression_flag(auto: pd.DataFrame, raw: pd.DataFrame,
                         scope: str = "website") -> pd.DataFrame:
    """Add `is_regression` from the Backlog tab's own baseline expansion.

    A row is regression when its (case, country, device) is an *automated*
    baseline row per the Backlog method — the single source of truth, so the
    solid segments reconcile with the Backlog / Coverage baseline numbers.
    Mobile App uses the priority-based MAPP baseline; other scopes the big_regr
    one.  Unspecified-device rows fall back to case-level membership.
    """
    if auto.empty:
        return auto
    try:
        from . import backlog_tab as bl
        loader = bl._mapp_backlog_data if scope == "mobile_app" else bl._backlog_data
        _, expanded_by_bu, _ = loader()
        frames = [f[f["category"] == "automated"] for f in expanded_by_bu.values()]
        base = (pd.concat(frames, ignore_index=True) if frames
                else pd.DataFrame(columns=["case_id", "country_label", "device"]))
    except Exception:                                                   # noqa: BLE001
        base = pd.DataFrame(columns=["case_id", "country_label", "device"])
    if base.empty:
        return auto.assign(is_regression=False)

    # Vectorised membership test — identical semantics to the previous
    # row-by-row comprehension (verified over randomised inputs), ~1.5× faster
    # on ~20k rows.  The bigger win is that `_load` is now cached, so this runs
    # once per data refresh instead of on every rerun.
    #   exact    → (case, country, device) is an automated baseline row
    #   fallback → device-less rows match on case-level membership
    out = auto.copy()
    cid = out["case_id"].astype(int)

    keys = base[["case_id", "country_label", "device"]].copy()
    keys["case_id"] = keys["case_id"].astype(int)
    keys = keys.drop_duplicates()          # keeps the merge 1:1 (no row blow-up)
    keys["_is_regr"] = True

    probe = pd.DataFrame({
        "case_id":       cid.to_numpy(),
        "country_label": out["country_label"].to_numpy(),
        "device":        out["device"].to_numpy(),
    })
    exact = (
        probe.merge(keys, on=["case_id", "country_label", "device"], how="left")
        ["_is_regr"].fillna(False).astype(bool).to_numpy()
    )
    fallback = ((out["device"] == "Unspecified")
                & cid.isin(set(keys["case_id"]))).to_numpy()
    out["is_regression"] = exact | fallback
    return out


@st.cache_data(ttl=21600, show_spinner=False)
def _load(scope: str) -> pd.DataFrame:
    """Automated rows for ONE scope, deduped on (bu, country, device, case_id),
    each carrying an `is_regression` flag so the chart can stack regression vs
    other automated tests.  Scope-driven so the Report reflects the selected
    radio (Website / Mobile App / Microservices) — Microservices no longer mixes
    into the Website view.

    Cached with the data TTL: the regression-flag join runs over every automated
    row, so without this it recomputed on every fragment rerun (BU switch, tab
    revisit).  Cleared by the ↻ refresh alongside the other data caches."""
    rules = [r for r in ALL_RULES if r.scope == scope]
    if not rules:
        return pd.DataFrame()
    result = evaluate_rules(tuple(r.name for r in rules))
    if result.automated.empty:
        return pd.DataFrame()
    auto = _add_regression_flag(result.automated, result.raw_cases, scope)
    return auto.drop_duplicates(subset=["bu", "country_label", "device", "case_id"])


# ── chart ─────────────────────────────────────────────────────────────────────
def _prepare_chart_data(auto: pd.DataFrame, bus: list[str]) -> pd.DataFrame:
    """Aggregate and annotate data for the Altair chart, split by regression flag.

    One row per (bu, country, device, category) where category ∈ {Regression, Other}.
    A 'total' column is set on exactly one row per (bu, country, device) so that
    the text mark renders the count once at the end of each stacked bar (the other
    rows carry total=0 and are removed via transform_filter).
    """
    if "is_regression" not in auto.columns:
        auto = auto.assign(is_regression=False)

    grp = (
        auto.groupby(["bu", "country_label", "device", "is_regression"])["case_id"]
        .nunique()
        .reset_index(name="count")
    )
    grp["category"]      = grp["is_regression"].map({True: "Regression", False: "Other"}).fillna("Other")
    grp["category_rank"] = grp["category"].map({"Regression": 0, "Other": 1}).astype(int)

    # One row per group carries the total — used by the text layer.
    totals_per_group = grp.groupby(["bu", "country_label", "device"])["count"].transform("sum")
    is_first         = ~grp.duplicated(subset=["bu", "country_label", "device"], keep="first")
    grp["total"]     = totals_per_group.where(is_first, 0)

    # Sort country alphabetically per BU
    grp["ctry_rank"] = (
        grp.groupby("bu")["country_label"]
        .transform(lambda s: s.map({c: i for i, c in enumerate(sorted(s.unique()))}))
    )
    grp["dev_rank"]  = grp["device"].map(
        {"iOS": 0, "Android": 1, "Mobile": 0, "Desktop": 1,
         "Unspecified": 2, "API": 2}).fillna(2).astype(int)
    grp["sort_key"]  = grp["ctry_rank"] * 10 + grp["dev_rank"]
    # Row label: MAPP rows are per-OS (country_label = BU, redundant with the
    # panel title) → show just "iOS"/"Android"; device-less rows (API /
    # Unspecified) → the country only; website rows → "device country".
    grp["label"] = grp.apply(
        lambda r: r["device"] if r["device"] in ("iOS", "Android")
        else (r["country_label"] if r["device"] in ("Unspecified", "API")
              else r["device"].lower() + " " + r["country_label"]),
        axis=1,
    )
    grp["bu_rank"] = grp["bu"].map({b: i for i, b in enumerate(bus)})

    return grp.rename(columns={"country_label": "country"})


def _ordered_bus(present: set[str]) -> list[str]:
    bus = [b for b in _BU_ORDER if b in present]
    return bus + sorted(b for b in present if b not in bus)


def _build_bu_chart(df_bu: pd.DataFrame) -> alt.LayerChart:
    """One responsive chart for a single BU (stacked bars + total labels).

    One chart per BU rendered inside `st.columns` (instead of a fixed-width
    Altair facet grid) so every panel is container-width-aware — the Report was
    the app's only non-responsive chart.
    Solid segment = regression baseline, faded = other automated.
    """
    y_sort = alt.EncodingSortField(field="sort_key", order="ascending")

    color_scale = alt.Scale(
        domain=["Mobile", "Desktop", "Unspecified", "API", "iOS", "Android"],
        range=[_BLUE, _ORANGE, _GREY, _GREY, _IOS, _ANDROID],   # API = grey, device-less
    )

    y_axis = alt.Axis(title=None, labelFontSize=11, labelFont="Inter",
                      labelColor=COLORS["text"],
                      labelLimit=170, ticks=False, domain=False)

    bars = (
        alt.Chart()
        .mark_bar(size=13, cornerRadiusEnd=3)
        .encode(
            x=alt.X("count:Q",
                    stack="zero",
                    axis=alt.Axis(title=None, grid=True, gridColor=COLORS["grid"],
                                  tickCount=5, labelFontSize=11, domain=False,
                                  labelColor=COLORS["muted"])),
            y=alt.Y("label:N", sort=y_sort, axis=y_axis),
            color=alt.Color("device:N", scale=color_scale, legend=None),
            opacity=alt.Opacity(
                "category:N",
                scale=alt.Scale(domain=["Regression", "Other"], range=[1.0, 0.40]),
                legend=None,
            ),
            order=alt.Order("category_rank:Q", sort="ascending"),
            tooltip=[
                alt.Tooltip("bu:N",       title="BU"),
                alt.Tooltip("country:N",  title="Country"),
                alt.Tooltip("device:N",   title="Device"),
                alt.Tooltip("category:N", title="Type"),
                alt.Tooltip("count:Q",    title="Count", format=","),
            ],
        )
    )

    # Text label at end of each stacked bar — only the row with total>0 renders.
    text = (
        alt.Chart()
        .mark_text(align="left", dx=5, fontSize=11, color=COLORS["text"])
        .encode(
            x=alt.X("total:Q"),
            y=alt.Y("label:N", sort=y_sort),
            text=alt.Text("total:Q", format=","),
        )
        .transform_filter(alt.datum.total > 0)
    )

    return (
        alt.layer(bars, text, data=df_bu)
        .properties(height=alt.Step(21), background="transparent")
        .configure_view(stroke=None, fill="transparent")
        .configure_axis(labelFont="Inter")
    )


# ── UI card helpers ───────────────────────────────────────────────────────────
def _leaderboard_chart(summary: pd.DataFrame) -> alt.LayerChart:
    """Executive glance: one RAG-coloured bar per BU, sorted by regression
    coverage %, with a dashed 80%-target line.  Same numbers as the Backlog tab
    and the KPI strip (single source of truth)."""
    d = summary[["BU", "Coverage %", "Automated", "Total"]].copy()
    d["cov"]   = d["Coverage %"].astype(float)
    d["color"] = d["cov"].map(lambda p: coverage_health(p)[1])
    d["label"] = d["cov"].map(lambda p: f"{p:.1f}%")
    order = d.sort_values("cov", ascending=False)["BU"].tolist()

    base = alt.Chart(d)
    bars = base.mark_bar(height=17, cornerRadiusEnd=3).encode(
        y=alt.Y("BU:N", sort=order,
                axis=alt.Axis(title=None, labelFontSize=12, labelFont="Inter",
                              labelColor=COLORS["ink"], labelLimit=180,
                              ticks=False, domain=False)),
        x=alt.X("cov:Q", scale=alt.Scale(domain=[0, 100]),
                axis=alt.Axis(title=None, grid=True, gridColor=COLORS["grid"],
                              values=[0, 20, 40, 60, 80, 100], format="d",
                              labelFontSize=11, domain=False,
                              labelColor=COLORS["muted"])),
        color=alt.Color("color:N", scale=None),
        tooltip=[alt.Tooltip("BU:N", title="BU"),
                 alt.Tooltip("cov:Q", title="Coverage %", format=".1f"),
                 alt.Tooltip("Automated:Q", title="Automated", format=","),
                 alt.Tooltip("Total:Q", title="Baseline rows", format=",")],
    )
    text = base.mark_text(align="left", dx=4, fontSize=11, font="Inter",
                          fontWeight="bold", color=COLORS["ink"]).encode(
        y=alt.Y("BU:N", sort=order), x=alt.X("cov:Q"), text="label:N")
    target = (
        alt.Chart(pd.DataFrame({"t": [COVERAGE_TARGET]}))
        .mark_rule(strokeDash=[4, 4], color=COLORS["muted"], size=1)
        .encode(x="t:Q")
    )
    return (
        (bars + text + target)
        .properties(height=max(130, len(d) * 30))
        .configure_view(strokeWidth=0)
    )


def _panel_header(bu: str, n_auto: int, cov: float | None) -> None:
    """Panel heading, in the same markup as the Coverage tab's chart panels, so
    a reader moving between the two tabs sees one component, not two."""
    sub = f"{n_auto:,} automated rows"
    if cov is not None:
        _dot, colour = coverage_health(cov)
        sub = (f"<span style='color:{colour};font-weight:700'>{cov:.1f}% Coverage"
               f"</span> · {sub}")
    st.markdown(
        f'<span class="cov-panel-head" style="height:auto">'
        f'<span class="cov-panel-title">{bu}</span>'
        f'<span class="cov-panel-sub">{sub}</span></span>',
        unsafe_allow_html=True,
    )

# ── render ────────────────────────────────────────────────────────────────────
def _backlog_badge_html(backlog: int, total: int) -> str:
    """The Backlog health pill, reused verbatim from the Backlog tab."""
    from .backlog_tab import _backlog_badge_html as _impl
    return _impl(backlog, total)


def _framework_line(summary, all_auto, scope: str, a_tot: dict) -> None:
    """Two muted lines replacing the three static framework cards.

    Those cards described the tooling ("AI-powered test automation platform")
    and carried no number.  The split matters: Java / TestIM / iOS / Android are
    BASELINE rows (they reconcile with the cards above), while the smoke suite
    and the automated total count the WHOLE automated set — mixing the two
    populations on one line is what makes numbers look wrong.
    """
    per_fw: list[str] = []
    cols = (("iOS", "Android") if scope == "mobile_app"
            else ("Java", "TestIM", "Playwright") if scope == "website" else ())
    for col in cols:
        if col not in summary:
            continue
        n = int(summary[col].sum())
        # Playwright is listed only once the migration produces rows; an empty
        # framework on an executive report reads as a broken number.
        if n == 0 and col == "Playwright":
            continue
        per_fw.append(f"{col} <b>{n:,}</b>")
    if per_fw:
        # Each row is attributed to ONE framework (the newest that covers it),
        # so these add up to Automated exactly.
        st.markdown(
            f"<div style='margin:4px 0 0;font-size:12px;color:{COLORS['muted']}'>"
            f"Baseline by framework: " + " &nbsp;·&nbsp; ".join(per_fw)
            + "</div>", unsafe_allow_html=True)

    try:
        smoke = int(metrics.totals(metrics.select_smoke(all_auto))["total"])
    except Exception:                                                   # noqa: BLE001
        smoke = 0
    extra = [f"{int(a_tot['total']):,} automated rows in total"]
    if smoke:
        extra.append(f"{smoke:,} in the smoke suite (Highest priority)")
    st.markdown(
        f"<div style='margin:2px 0 0;font-size:12px;color:{COLORS['faint']}'>"
        f"Beyond the baseline: " + " &nbsp;·&nbsp; ".join(extra) + "</div>",
        unsafe_allow_html=True,
    )


@st.fragment
def render() -> None:
    # Standard tab opener (subheader + caption) — same pattern as every other tab.
    # Section title removed (redundant with the "Report" tab label).
    # Scope-driven: the Report reflects the selected radio (Website / Mobile App
    # / Microservices).  Microservices no longer mixes into the Website view.
    scope, _bu = global_filter.current()
    scope_lbl  = global_filter.scope_label(scope)
    is_mapp    = scope == "mobile_app"

    st.caption(f"Automation baseline per Business Unit · **{scope_lbl}**.")

    with st.spinner("📱 Loading Mobile App report — first load can take ~30-60s, "
                    "then it's cached…" if is_mapp else "Loading…"):
        all_auto = _load(scope)

    if all_auto.empty:
        st.warning(
            f"No automated data for **{scope_lbl}** yet — data refreshes "
            "automatically every few hours (or use the ↻ next to the tabs)."
        )
        return

    a_tot = metrics.totals(all_auto)
    summary = _scope_summary(scope)

    # ── Header: the same five cards, in the same words, as the Backlog tab ────
    # It used to open with three framework "cards" carrying marketing blurbs and
    # no data, plus two badges in a typography nothing else in the app uses.  An
    # executive now reads the baseline in the vocabulary they already know.
    if not summary.empty and "Total" in summary.columns:
        _tot  = int(summary["Total"].sum())
        _auto = int(summary["Automated"].sum())
        _back = int(summary["Backlog"].sum())
        _part = int(summary["Partially Automated"].sum()) \
            if "Partially Automated" in summary else 0
        _tbu  = int(summary["To Update"].sum())
        _na   = int(summary["Not Applicable"].sum())
        _unk  = int(summary["Unknown"].sum()) if "Unknown" in summary else 0
        # "Automatable" excludes Not Applicable AND Unknown — the same
        # denominator `backlog_tab._stats` uses, so the two tabs agree.
        _automatable = _auto + _back + _part + _tbu
        cov     = (_auto / _tot * 100) if _tot else 0.0
        cov_aut = (_auto / _automatable * 100) if _automatable else 0.0
        na_pct  = (_na / (_automatable + _na) * 100) if (_automatable + _na) else 0.0

        cards = [("Total", _tot, ""), ("Automated", _auto, ""),
                 ("Backlog", _back, _backlog_badge_html(_back, _tot))]
        if _part:
            cards.append(("Partially Automated", _part, ""))
        cards += [("To Update", _tbu, ""), ("Not Applicable", _na, "")]
        if _unk:
            cards.append(("Unknown", _unk, ""))
        cols = st.columns(len(cards))
        for col, (label, n, badge) in zip(cols, cards):
            stat_card(col, label, n, None, badge_html=badge)

        below = [r for _, r in summary.iterrows()
                 if float(r.get("Coverage %") or 0) < COVERAGE_TARGET]
        target_note = (
            f" &nbsp;·&nbsp; <span style='color:{COLORS['danger']};font-weight:600'>"
            f"{len(below)} of {len(summary)} Business Units below the "
            f"{COVERAGE_TARGET:.0f}% target</span>"
            if len(summary) > 1 and below else ""
        )
        st.markdown(
            f"<div style='margin:10px 0 2px;font-size:13px;color:{COLORS['text']}'>"
            f"<b>Coverage</b> <code>{cov:.1f}%</code> &nbsp;·&nbsp; "
            f"<b>Coverage vs Automatable</b> <code>{cov_aut:.1f}%</code> &nbsp;·&nbsp; "
            f"<b>Not Applicable</b> <code>{na_pct:.1f}%</code>{target_note}</div>",
            unsafe_allow_html=True,
        )
        _framework_line(summary, all_auto, scope, a_tot)

    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    # ── EXECUTIVE SUMMARY: coverage leaderboard (scope-filtered) ──────────────
    cov_by_bu: dict[str, float] = {}
    if not summary.empty and "Coverage %" in summary.columns and len(summary) > 1:
        cov_by_bu = {str(r["BU"]): float(r["Coverage %"]) for _, r in summary.iterrows()}
        section_title("Coverage by Business Unit")
        st.altair_chart(_leaderboard_chart(summary), width="stretch")
        st.caption(
            f"Automated share of the baseline per BU — same numbers as the "
            f"Backlog tab. Dashed line = {COVERAGE_TARGET:.0f}% target."
        )
        st.markdown("<div style='height:22px'></div>", unsafe_allow_html=True)
    elif not summary.empty and "Coverage %" in summary.columns:
        # single BU (e.g. Microservices) — no leaderboard, still annotate panels
        cov_by_bu = {str(r["BU"]): float(r["Coverage %"]) for _, r in summary.iterrows()}

    # ── DETAIL: automated tests by device ─────────────────────────────────────
    def _dot(color: str) -> str:
        return (f'<span style="display:inline-block;width:11px;height:11px;'
                f'border-radius:2px;background:{color};margin-right:5px;'
                f'vertical-align:middle"></span>')

    if is_mapp:
        devs_legend = (f'{_dot(_IOS)}<span>iOS</span>'
                       f'{_dot(_ANDROID)}<span>Android</span>')
    else:
        devs_legend = (f'{_dot(_BLUE)}<span>Mobile</span>'
                       f'{_dot(_ORANGE)}<span>Desktop</span>'
                       f'{_dot(_GREY)}<span style="color:{COLORS["muted"]}">'
                       f'Unspecified / API</span>')
    legend_html = (
        f'<span style="display:flex;align-items:center;justify-content:flex-end;'
        f'gap:16px;font-size:12px;color:{COLORS["text"]}">{devs_legend}'
        f'<span style="color:{COLORS["muted"]};font-size:11px;'
        f'border-left:1px solid {COLORS["border"]};padding-left:10px">'
        f'solid = baseline&nbsp;·&nbsp;faded = other</span>'
        f'</span>'
    )
    c_head, c_legend = st.columns([2, 3], vertical_alignment="center")
    with c_head:
        section_title("Automated Tests by Device", top=0)
    with c_legend:
        st.markdown(legend_html, unsafe_allow_html=True)

    # ── Charts — one responsive panel per BU, in aligned 2-column rows ────────
    bus = _ordered_bus(set(all_auto["bu"].unique()))
    df  = _prepare_chart_data(all_auto, bus)
    # Unique automated cases per BU (NOT the sum of the per-country bars — a
    # multi-country case is counted once).
    auto_by_bu = all_auto.groupby("bu")["case_id"].nunique().to_dict()
    # Sort panels by how many (country × device) bars they carry, tallest first,
    # so adjacent 2-column panels are close in height and the grid has no big
    # empty gaps (a 16-row BU no longer sits next to a 2-row one).
    rows_by_bu = (df.drop_duplicates(subset=["bu", "country", "device"])
                    .groupby("bu").size().to_dict())
    bus = sorted(bus, key=lambda b: -rows_by_bu.get(b, 0))
    for row_start in range(0, len(bus), 2):
        cols = st.columns(2, gap="large")
        for col, bu in zip(cols, bus[row_start:row_start + 2]):
            with col:
                _panel_header(bu, int(auto_by_bu.get(bu, 0)), cov_by_bu.get(bu))
                st.altair_chart(_build_bu_chart(df[df["bu"] == bu]),
                                width="stretch")
