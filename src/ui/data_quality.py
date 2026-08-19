"""Data-quality panel — the TestRail hygiene checklist, computed from cached data.

Surfaces the anomaly classes that were only discoverable by manual digging, so
the team gets an actionable clean-up list and the dashboard's numbers keep
their credibility in front of management:

  1. **Baseline cases with no country token** — big_regr-labelled cases whose
     `multi_countries` matches no BU on their suite (the C4414081 class):
     they are invisible in every BU's baseline.
  2. **Cases not attributable to any BU** — same token problem on the whole
     (non-deprecated) universe of shared suites: counted by no one.
  3. **Suspicious areas still holding cases** — sections named like
     "to be deleted" / "[DELETE]" / "deprecated" that still contain active cases.
  4. **Unknown baseline rows** — rows whose status says automated but that are
     not attributed to the BU's automated set (or carry no status at all).

Everything is derived from the frames the dashboard already caches — the scan
adds ZERO TestRail calls.  Rendered as an expander at the bottom of the Backlog
tab with a CSV download for the clean-up work.

Scope-aware: checks 1 & 2 need country tokens, so they only produce findings for
Website / Microservices; Mobile App (no country dimension) gets checks 3 & 4.
"""
from __future__ import annotations

import logging
import re

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)

# Section names that should not hold active cases.
_SUSPICIOUS_AREA_RE = re.compile(
    r"to be delete|to delete|\[delete\]|deprecated|do not use|obsolete|trash",
    re.IGNORECASE,
)


def _tokens(mc) -> set[str]:
    return set(mc) if isinstance(mc, list) else set()


@st.cache_data(ttl=21600, show_spinner=False)
def _scan(scope: str = "website") -> dict[str, pd.DataFrame]:
    """All applicable checks for *scope*.  Raises on failure so st.cache_data
    never caches an error (retried next rerun).

    The country-token checks (1 & 2) only make sense where cases carry country
    tokens — Mobile App has no country dimension, so for that scope only the
    suspicious-areas and unknown-rows checks run."""
    from .backlog_tab import _load_scope

    raw, _auto, rules = _load_scope(scope)   # raw is already non-deprecated
    out: dict[str, pd.DataFrame] = {}

    # Token universe per suite (union across every BU sharing it).
    suite_tokens: dict[int, set[str]] = {}
    suite_bus:    dict[int, set[str]] = {}
    for r in rules:
        suite_tokens.setdefault(r.suite_id, set()).update(r.countries_filter or [])
        suite_bus.setdefault(r.suite_id, set()).add(r.bu)

    has_cols = {"labels", "multi_countries", "suite_id", "case_id"} <= set(raw.columns)
    no_token_rows, orphan_rows = [], []
    if not raw.empty and has_cols:
        for sid, all_toks in suite_tokens.items():
            if not all_toks:
                continue                       # single-country suite: token-free by design
            sub = raw[raw["suite_id"] == sid]
            if sub.empty:
                continue
            # `toks=all_toks` binds the loop variable at definition time.
            unmatched = sub[~sub["multi_countries"].apply(
                lambda mc, toks=all_toks: bool(_tokens(mc) & toks))]
            bus_lbl = " / ".join(sorted(suite_bus.get(sid, set())))
            # to_dict("records") instead of iterrows: shared suites can have
            # thousands of unmatched rows, and iterrows is ~100× slower.
            cols = [c for c in ("case_id", "title", "multi_countries",
                                "labels", "url") if c in unmatched.columns]
            for row in unmatched[cols].to_dict("records"):
                labels = row.get("labels")
                rec = {
                    "case_id":         int(row["case_id"]),
                    "title":           str(row.get("title", ""))[:80],
                    "suite":           f"{sid} ({bus_lbl})",
                    "multi_countries": ", ".join(_tokens(row.get("multi_countries")))
                                       or "(empty)",
                    "url":             row.get("url", ""),
                }
                is_baseline = isinstance(labels, list) and (
                    "big_regr_desktop" in labels or "big_regr_mobile" in labels)
                (no_token_rows if is_baseline else orphan_rows).append(rec)
    out["baseline_no_token"] = pd.DataFrame(no_token_rows)
    out["orphan_cases"]      = pd.DataFrame(orphan_rows)

    # Suspicious areas still holding active cases.
    area_rows = []
    if not raw.empty and "section_path" in raw.columns:
        sus = raw[raw["section_path"].fillna("").str.contains(_SUSPICIOUS_AREA_RE)]
        if not sus.empty:
            grouped = (sus.groupby(["suite_id", "section_path"])["case_id"]
                       .nunique().reset_index(name="cases"))
            for _, row in grouped.iterrows():
                bus_lbl = " / ".join(sorted(suite_bus.get(int(row["suite_id"]), set())))
                area_rows.append({
                    "suite":   f"{int(row['suite_id'])} ({bus_lbl})",
                    "area":    str(row["section_path"]),
                    "cases":   int(row["cases"]),
                })
    out["suspicious_areas"] = pd.DataFrame(area_rows)

    # Unknown rows in the regression baseline.
    unknown_rows = []
    from . import backlog_tab as bl
    loader = bl._mapp_backlog_data if scope == "mobile_app" else bl._backlog_data
    _summary, expanded_by_bu, _auto_by_bu = loader()
    for (bu, bu_scope), exp in expanded_by_bu.items():   # bu_scope: don't shadow `scope`
        unk = exp[exp["category"] == "unknown"] if "category" in exp.columns else exp.iloc[0:0]
        if not unk.empty:
            ids = sorted(unk["case_id"].astype(int).unique())
            unknown_rows.append({
                "bu":       f"{bu} ({bu_scope})",
                "rows":     int(len(unk)),
                "cases":    ", ".join(f"C{i}" for i in ids[:15])
                            + (" …" if len(ids) > 15 else ""),
            })
    out["unknown_rows"] = pd.DataFrame(unknown_rows)

    out["playwright_migration"] = _playwright_migration(raw)

    return out


def _playwright_migration(raw: pd.DataFrame) -> pd.DataFrame:
    """Cases labelled `playwright` whose status fields do not agree with it.

    The label is what makes a case count as Playwright, but it is NOT what makes
    it count as automated — that still comes from a status field.  So a labelled
    case with no automated status silently sits in the Backlog, and one that
    still carries a Testim status is a half-finished migration.  Both are
    invisible in every other view, which is exactly why they belong here.

    Returns an empty frame until the migration actually starts, so the panel is
    unchanged for as long as no case carries the label.
    """
    # Both constants come from the modules that OWN them, so the check can never
    # drift from the engine it is checking.
    from ..rules_engine import _PLAYWRIGHT_LABEL
    from .backlog_tab import _STATUS_AUTO

    cols = {"labels", "case_id"}
    if raw.empty or not cols <= set(raw.columns):
        return pd.DataFrame()

    labelled = raw[raw["labels"].apply(
        lambda ls: isinstance(ls, list)
        and any(str(x).strip().lower() == _PLAYWRIGHT_LABEL for x in ls))]
    if labelled.empty:
        return pd.DataFrame()

    status_cols = [c for c in labelled.columns if c.startswith("status_")]
    testim_cols = [c for c in status_cols if "Testim" in c]

    def _filled(row, columns) -> list[str]:
        return [c for c in columns
                if pd.notna(row.get(c)) and str(row.get(c)).strip()]

    rows = []
    for row in labelled.to_dict("records"):
        automated = [c for c in _filled(row, status_cols)
                     if str(row[c]).strip() in _STATUS_AUTO]
        stale     = [c for c in _filled(row, testim_cols)]
        if automated and not stale:
            continue                                  # clean migration
        problem = ("labelled playwright but no automated status — counts as "
                   "Backlog, not as Playwright" if not automated
                   else "still carries a Testim status — clean the old field")
        rows.append({
            "case_id": int(row["case_id"]),
            "title":   str(row.get("title", ""))[:80],
            "problem": problem,
            "fields":  ", ".join(c.removeprefix("status_")
                                 for c in (stale or _filled(row, status_cols)))
                       or "(none filled)",
            "url":     row.get("url", ""),
        })
    return pd.DataFrame(rows)



# ── why a baseline row is Unknown ────────────────────────────────────────────
# Stable strings: they are the grouping key of the summary sheet, and the whole
# point of that sheet is that one fix clears a whole batch.
_R_NO_STATUS   = "No automation status on the case"
_R_WRONG_FIELD = "Automated in a field this BU's rules do not read"
_R_COUNTRY_EMPTY = "Automated, but the country field the rule reads is empty"
_R_COUNTRY_OTHER = "Automated, but the country field lists no country of this BU"
_R_COUNTRY_MISS  = "Automated, but not for this row's country"
_R_MISSING_LABEL = "Automated, but the rule reading that field needs a label the case lacks"
_R_NOT_AUTO      = "No status field carries an automated value"

# rule.country_field_label → the raw_cases column holding it
_COUNTRY_COL = {
    "multi_countries":          "multi_countries",
    "custom_country_coverage":  "country_coverage",
    "Testim Country Coverage":  "testim_country_coverage",
    "Java Country Coverage":    "java_country_coverage",
    "Playwright Country Coverage": "playwright_country_coverage",
    "Country Validation":       "country_validation",
}


def _tok_list(v) -> list[str]:
    return [str(t) for t in v] if isinstance(v, list) else []


def _unknown_detail(bu: str, scope: str) -> pd.DataFrame:
    """Every Unknown baseline row of *bu*, with the reason it is Unknown and the
    TestRail fields that reason was read from.

    Built on demand — nobody pays for it unless they click the download.
    """
    from . import backlog_tab as bl

    loader = bl._mapp_backlog_data if scope == "mobile_app" else bl._backlog_data
    try:
        _summary, expanded_by_bu, _auto = loader()
        raw, _a, rules = bl._load_scope(scope)
    except Exception:                                                   # noqa: BLE001
        logger.exception("Unknown-reason detail failed")
        return pd.DataFrame()

    exp = expanded_by_bu.get((bu, scope))
    if exp is None or exp.empty or "category" not in exp.columns:
        return pd.DataFrame()
    unk = exp[exp["category"] == "unknown"]
    if unk.empty or raw.empty:
        return pd.DataFrame()

    rules_bu = [r for r in rules if r.bu == bu]
    read_status = {r.status_field_label for r in rules_bu}
    bu_tokens   = {t for r in rules_bu for t in r.countries_filter}
    status_cols = [c for c in raw.columns if c.startswith("status_")]
    meta_df = raw.drop_duplicates("case_id").copy()
    meta_df["case_id"] = meta_df["case_id"].astype(int)
    meta = meta_df.set_index("case_id").to_dict("index")

    rows = []
    for cid, country, device in zip(unk["case_id"].astype(int),
                                    unk["country_label"], unk["device"]):
        case = meta.get(cid, {})
        filled = {c.removeprefix("status_"): str(case[c]).strip()
                  for c in status_cols
                  if case.get(c) is not None and str(case.get(c)).strip()}
        auto_filled = {k: v for k, v in filled.items() if v in bl._STATUS_AUTO}

        # Rules whose status field carries an automated value on this case, and
        # of those the ones whose label gate is satisfied.  Splitting the two
        # matters: the Playwright rules read the generic "Automation Status" but
        # only for cases carrying the `playwright` label, so a case can have the
        # field a rule reads and still be out of that rule's reach.
        case_labels = {t.strip().lower() for t in _tok_list(case.get("labels"))}
        cand = [r for r in rules_bu
                if filled.get(r.status_field_label, "") in bl._STATUS_AUTO]
        open_rules = [r for r in cand
                      if all(w.strip().lower() in case_labels
                             for w in getattr(r, "labels_filter", []))]

        rule   = open_rules[0] if open_rules else None
        cfield = rule.country_field_label if rule else ""
        ctoks  = _tok_list(case.get(_COUNTRY_COL.get(cfield, ""), []))
        read_auto = bool(open_rules)

        if not filled:
            reason, detail = _R_NO_STATUS, ""
        elif auto_filled and not cand:
            reason = _R_WRONG_FIELD
            detail = (f"{', '.join(f'{k} = {v}' for k, v in auto_filled.items())}"
                      f" · this BU is decided by: {', '.join(sorted(read_status))}")
        elif cand and not open_rules:
            reason = _R_MISSING_LABEL
            needed = sorted({w for r in cand for w in getattr(r, "labels_filter", [])})
            detail = (f"{cand[0].status_field_label} = "
                      f"{filled[cand[0].status_field_label]} · needs label(s): "
                      f"{', '.join(needed)}")
        elif read_auto and not ctoks:
            reason = _R_COUNTRY_EMPTY
            detail = f"{cfield} is empty"
        elif read_auto and not (set(ctoks) & bu_tokens):
            reason = _R_COUNTRY_OTHER
            detail = f"{cfield} = {', '.join(ctoks)} · {bu} uses {', '.join(sorted(bu_tokens))}"
        elif read_auto:
            reason = _R_COUNTRY_MISS
            detail = f"{cfield} = {', '.join(ctoks)} · this row is {country}"
        else:
            reason = _R_NOT_AUTO
            detail = ", ".join(f"{k} = {v}" for k, v in filled.items())

        rows.append({
            "Case ID": f"C{cid}",
            "Title":   str(case.get("title", ""))[:120],
            "Country": country, "Device": device,
            "Reason":  reason,
            "Evidence": detail,
            "Section": case.get("section_path", ""),
            "Priority": case.get("priority_label", ""),
            "Labels": ", ".join(_tok_list(case.get("labels"))),
            "multi_countries": ", ".join(_tok_list(case.get("multi_countries"))),
            "Testim Country Coverage": ", ".join(_tok_list(case.get("testim_country_coverage"))),
            "Java Country Coverage": ", ".join(_tok_list(case.get("java_country_coverage"))),
            "Playwright Country Coverage": ", ".join(
                _tok_list(case.get("playwright_country_coverage"))),
            "Country Validation": ", ".join(_tok_list(case.get("country_validation"))),
            **{k: v for k, v in filled.items()},
            "TestRail Link": case.get("url", ""),
        })
    return pd.DataFrame(rows)


def _unknown_reason_summary(detail: pd.DataFrame) -> pd.DataFrame:
    """Rows and cases per reason — the sheet a lead fixes a batch from."""
    if detail.empty:
        return pd.DataFrame()
    g = (detail.groupby("Reason")
         .agg(Rows=("Case ID", "size"), Cases=("Case ID", "nunique"))
         .reset_index().sort_values("Rows", ascending=False))
    g["Example cases"] = [
        ", ".join(sorted(detail.loc[detail["Reason"] == r, "Case ID"].unique())[:10])
        for r in g["Reason"]
    ]
    g["Share of rows"] = (g["Rows"] / len(detail) * 100).round(1).astype(str) + "%"
    return g[["Reason", "Rows", "Cases", "Share of rows", "Example cases"]]


def _unknown_workbook(bu: str, scope: str):
    """Deferred payload: a two-sheet workbook, summary first.

    Falls back to a single CSV if no Excel engine is installed on the host —
    the file is the point, the format is not worth an exception in front of a
    user who just clicked Download.
    """
    def _build() -> bytes:
        detail  = _unknown_detail(bu, scope)
        summary = _unknown_reason_summary(detail)
        if detail.empty:
            return b""
        try:
            import io
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xl:
                summary.to_excel(xl, sheet_name="Reasons", index=False)
                detail.to_excel(xl, sheet_name="Rows", index=False)
            return buf.getvalue()
        except Exception:                                               # noqa: BLE001
            logger.exception("Excel export unavailable, falling back to CSV")
            return detail.to_csv(index=False).encode("utf-8")
    return _build


_CHECKS = [
    ("baseline_no_token", "🚨 Baseline cases with no country token",
     "big_regr cases whose `multi_countries` matches no BU on their suite — "
     "invisible in EVERY baseline. Fix the field in TestRail."),
    ("orphan_cases", "👻 Cases not attributable to any BU",
     "Non-deprecated cases on shared suites with no matching country token — "
     "counted by no BU anywhere in the dashboard."),
    ("suspicious_areas", "🗑 Suspicious areas still holding cases",
     "Sections named like 'to be deleted' / 'deprecated' that still contain "
     "active cases — they pollute area breakdowns and coverage."),
    ("playwright_migration", "🎭 Playwright migration to finish",
     "Cases labelled `playwright` whose status fields disagree with the label: "
     "no automated status (so they count as Backlog, not Playwright), or a "
     "Testim status left behind by the migration."),
    ("unknown_rows", "❓ Unknown baseline rows",
     "Baseline rows whose case is automated NOWHERE, yet no status explains "
     "them — an automated-looking value in a field this BU's rules do not read, "
     "a country field left empty, or no status at all.  (A case automated on "
     "some other country/device counts as Partially Automated, not here.)"),
]


def finding_count(scope: str = "website") -> int | None:
    """How many hygiene findings this scope has — None when it can't be computed
    (e.g. the data isn't warm yet).  Used to badge the utility-bar label."""
    try:
        return sum(len(df) for df in _scan(scope).values())
    except Exception:                                                   # noqa: BLE001
        logger.exception("Data-quality count failed")
        return None


def _unknown_downloads(df: pd.DataFrame, scope: str) -> None:
    """One workbook per BU, built only when its button is clicked.

    Unknown rows are the hardest finding to act on: the table says how many
    there are, not why.  The workbook opens on the reason breakdown precisely
    because the reasons repeat — one wrong field on a suite explains dozens of
    rows, and a lead fixes those in a single pass rather than case by case.
    """
    st.caption("Per business unit — sheet 1 groups the reasons, sheet 2 lists "
               "every row with the TestRail fields behind it.")
    # A wrapping flex row, one button per table row and in the same order, so a
    # reader goes straight from the number to the file behind it.  No column
    # arithmetic: the row reflows on its own however many BUs are listed.
    with st.container(key=f"dq_unk_row_{scope}", horizontal=True,
                      vertical_alignment="center", gap="small"):
        for _, r in df.iterrows():
            bu = str(r["bu"]).rsplit(" (", 1)[0]
            st.download_button(
                f"⬇ {bu} · {int(r['rows']):,}",
                _unknown_workbook(bu, scope),
                file_name=f"{bu.replace(' ', '_')}_unknown_rows.xlsx",
                mime="application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet",
                key=f"dq_unk_{scope}_{bu}",
                help=f"Every Unknown row of {bu}, each with the reason it could "
                     f"not be classified and the fields that reason was read "
                     f"from.",
            )


def render_body(scope: str = "website") -> None:
    """The panel contents WITHOUT a container, so the caller decides whether it
    lives in a popover (utility bar) or an expander."""
    # Marker class — `[data-testid="stPopoverBody"]:has(.dq-panel)` in styles.py
    # widens the popover for this panel only (the chat popover keeps its own).
    st.markdown('<div class="dq-panel"></div>', unsafe_allow_html=True)
    try:
        data = _scan(scope)
    except Exception:                                                   # noqa: BLE001
        logger.exception("Data-quality scan failed")
        st.caption("Could not run the hygiene checks right now.")
        return

    st.caption(
        "TestRail hygiene checks computed from the data already loaded "
        "(no extra API calls). Fixing these keeps every number above credible."
    )
    total = sum(len(df) for df in data.values())
    if not total:
        st.success("No hygiene issues detected 🎉")
        return

    frames_for_csv: list[pd.DataFrame] = []
    for key, title, desc in _CHECKS:
        df = data.get(key)
        if df is None or df.empty:
            continue
        st.markdown(f"**{title}** · {len(df)}")
        st.caption(desc)
        col_cfg = {}
        if "url" in df.columns:
            col_cfg["url"] = st.column_config.LinkColumn(
                "Open", display_text="↗", width="small")
        st.dataframe(df, width="stretch", hide_index=True, column_config=col_cfg)
        if key == "unknown_rows":
            _unknown_downloads(df, scope)
        frames_for_csv.append(df.assign(check=key))

    combined = pd.concat(frames_for_csv, ignore_index=True)
    st.download_button(
        "⬇️ Download clean-up list (CSV)",
        combined.to_csv(index=False).encode("utf-8"),
        file_name=f"testrail_data_quality_{scope}.csv",
        mime="text/csv",
    )
