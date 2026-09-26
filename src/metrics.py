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
