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
def _scan() -> dict[str, pd.DataFrame]:
    """All checks over the cached website-scope frames.  Raises on failure so
    st.cache_data never caches an error (retried next rerun)."""
    from . import backlog_tab as bl

    raw, _auto, rules = bl._load_scope("website")   # raw is already non-deprecated
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
            unmatched = sub[~sub["multi_countries"].apply(
                lambda mc: bool(_tokens(mc) & all_toks))]
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
    _summary, expanded_by_bu, _auto_by_bu = bl._backlog_data()
    for (bu, scope), exp in expanded_by_bu.items():
        unk = exp[exp["category"] == "unknown"] if "category" in exp.columns else exp.iloc[0:0]
        if not unk.empty:
            ids = sorted(unk["case_id"].astype(int).unique())
            unknown_rows.append({
                "bu":       f"{bu} ({scope})",
                "rows":     int(len(unk)),
                "cases":    ", ".join(f"C{i}" for i in ids[:15])
                            + (" …" if len(ids) > 15 else ""),
            })
    out["unknown_rows"] = pd.DataFrame(unknown_rows)

    return out


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
    ("unknown_rows", "❓ Unknown baseline rows",
     "Baseline rows with an automated-looking status not attributed to the "
     "BU's automated set (usually a country mismatch), or no status at all."),
]


def render() -> None:
    """The '🧹 Data quality' expander — call at the bottom of the Backlog tab."""
    try:
        data = _scan()
    except Exception:                                                   # noqa: BLE001
        logger.exception("Data-quality scan failed")
        return

    total = sum(len(df) for df in data.values())
    label = (f"🧹 Data quality — {total} finding{'s' if total != 1 else ''} to clean up"
             if total else "🧹 Data quality — all clean")
    with st.expander(label, expanded=False):
        st.caption(
            "TestRail hygiene checks computed from the data already loaded "
            "(no extra API calls). Fixing these keeps every number above credible."
        )
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
            st.dataframe(df, width="stretch", hide_index=True,
                         column_config=col_cfg)
            frames_for_csv.append(df.assign(check=key))

        combined = pd.concat(frames_for_csv, ignore_index=True)
        st.download_button(
            "⬇️ Download clean-up list (CSV)",
            combined.to_csv(index=False).encode("utf-8"),
            file_name="testrail_data_quality.csv",
            mime="text/csv",
        )
