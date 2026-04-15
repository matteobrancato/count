"""Compute the aggregated counts shown in the Overview tab.

Input is the `automated` DataFrame produced by `rules_engine.evaluate_rules`.
Each row is already one (case × country × device) expansion that passed its rule,
so counts are simply `len(subset)` after slicing — no re-evaluation needed.

Deduplication note:
    Within a single BU, a case automated by multiple frameworks (e.g. Java AND
    TestIM Desktop) must only be counted ONCE for the "No-Regression" total.
    We dedupe on (bu, country_label, device, case_id) before counting.

    Smoke (Highest only) and Prod Sanity use the same dedupe keys plus a
    priority / flag filter.
"""
from __future__ import annotations

import pandas as pd


# --------------------------------------------------------------------- selectors
def _dedupe(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df.drop_duplicates(subset=["bu", "country_label", "device", "case_id"])


def select_regression(df: pd.DataFrame) -> pd.DataFrame:
    return _dedupe(df)


def select_smoke(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df["priority_label"].fillna("").str.lower().str.contains("highest")
    return _dedupe(df[mask])


def select_prod_sanity(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return _dedupe(df[df["is_prod_sanity"] == True])  # noqa: E712


# --------------------------------------------------------------------- breakdowns
def breakdown_by(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=by + ["count"])
    return (
        df.groupby(by, dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values(by)
        .reset_index(drop=True)
    )


def totals(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total": 0, "desktop": 0, "mobile": 0}
    return {
        "total": int(len(df)),
        "desktop": int((df["device"] == "Desktop").sum()),
        "mobile": int((df["device"] == "Mobile").sum()),
    }


# --------------------------------------------------------------------- coverage
def coverage_by_section(
    raw: pd.DataFrame, automated: pd.DataFrame, *, section_level: int = 1
) -> pd.DataFrame:
    """% automated per section (at a given hierarchy depth).

    `raw` is every case in the relevant suites (non-deprecated only). `automated`
    is the expansion DataFrame; we dedupe on case_id for coverage purposes so a
    case counted by multiple rules only contributes once to "automated".
    """
    if raw.empty:
        return pd.DataFrame(columns=["section", "total", "automated", "coverage"])

    base = raw[raw["deprecated"] == False].copy()  # noqa: E712

    def top_sections(path: str) -> str:
        parts = [p.strip() for p in (path or "").split(">") if p.strip()]
        if not parts:
            return "(root)"
        return " > ".join(parts[:section_level])

    base["section"] = base["section_path"].map(top_sections)

    auto_ids = set(automated["case_id"].unique()) if not automated.empty else set()
    base["is_auto"] = base["case_id"].isin(auto_ids)

    grouped = (
        base.groupby("section", dropna=False)
        .agg(total=("case_id", "count"), automated=("is_auto", "sum"))
        .reset_index()
    )
    grouped["coverage"] = (grouped["automated"] / grouped["total"]).fillna(0.0)
    return grouped.sort_values("total", ascending=False).reset_index(drop=True)
