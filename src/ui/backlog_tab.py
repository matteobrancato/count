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
  Website BUs  : multi_countries filtered to BU tokens
  Microservices     : custom_country_coverage filtered to ALL_COUNTRY_TOKENS

Classification (per expanded row)
──────────────────────────────────
  Precedence, first match wins:
  to_be_updated  → any status field = "To be updated".  Beats `automated`: the
                   tool fields say whether a script EXISTS, the manual QAs write
                   this one when the test itself has changed, so a script that
                   no longer matches its test is work to do, not coverage.  The
                   row keeps `_automated_row` so To be Updated can report how much
                   of it is maintenance rather than new automation.
  automated      → (case_id, country_label, device) is in evaluate_rules().automated
  not_applicable → any status field = "Automation not applicable"
  backlog        → any other non-empty, non-automated status, and the case is
                   automated NOWHERE
  partially_auto → same, or no status at all, but the case IS automated on some
                   other country / device — only that combination is missing
  unknown        → no status explains the row AND the case is automated nowhere

Counts
──────
  Expanded  = one row per (case_id × country_label × device) — shown as main number
  Unique    = distinct case_id values within each category — shown in small text below

Scopes
──────
  website / next_gen : the big_regr label baseline described above.
  mobile_app         : a PRIORITY-based baseline (High/Highest) with the mobile
                       OS as device — see `_expand_mapp_baseline`.  It is served
                       by its own `_mapp_backlog_data()` so the website /
                       microservices numbers (KPI strip, Report, Dexter) are
                       never affected by it.
"""
from __future__ import annotations

import html
import re

import pandas as pd
import streamlit as st

from ..bu_rules import (
    ALL_RULES,
    MOBILE_APP_BUS,
    PROD_SANITY_LABEL,
    WEBSITE_BUS,
    filter_conditional_tokens,
)
from .. import testrail_client as tr
from ..rules_engine import evaluate_rules
from . import global_filter
from .styles import (
    COLORS,
    COVERAGE_TARGET,
    coverage_health,
    section_title,
    stat_card,
)

# ── constants ─────────────────────────────────────────────────────────────────
# Baseline labels (website regression: desktop / mobile BROWSER view).
# Mobile App deliberately does NOT use these: it has no big_regr label, so its
# baseline is priority-based (see `_MAPP_PRIORITIES` / `_expand_mapp_baseline`).
_LABEL_DESKTOP = "big_regr_desktop"
_LABEL_MOBILE  = "big_regr_mobile"

# Production Sanity: an INDEPENDENT baseline that may overlap the regression one.
# A case carrying both labels is counted in both — "100 automated, 5 of them
# prod sanity" means 100 and 5, not 95 and 5.
#
# One definition, shared with the engine: the same label also drives
# `is_prod_sanity`, so the Coverage tab's Production Sanity view and the
# baseline here can never disagree.
_LABEL_PROD_SANITY = PROD_SANITY_LABEL

_STATUS_AUTO: set[str] = {
    "Automated", "Automated DEV", "Automated UAT", "Automated Prod",
}
_STATUS_NA: set[str] = {
    "Automation not applicable",
}
# "To be updated" — a test that was automated but needs maintenance.  Split out
# of Backlog into its own category.  Matched NORMALISED (strip + lowercase) so
# casing/spacing variants of the TestRail label still hit.
_STATUS_TO_UPDATE: set[str] = {
    "to be updated",
}

# Mobile-App baseline membership: cases with one of these priorities (there is
# no big_regr label for MAPP — priority IS the baseline definition).  Matched
# normalised (strip + lowercase).
_MAPP_PRIORITIES: set[str] = {"high", "highest"}


# Which status field owns a device row.  Used both when classifying (a TestIM
# case is judged per device) and when explaining that classification in the
# export, so the two can never tell different stories.
_DEVICE_STATUS_COL = {
    "Desktop": "status_Automation Status Testim Desktop",
    "Mobile":  "status_Automation Status Testim Mobile View",
}


def _read_status_cols(raw: pd.DataFrame, rules: list) -> list[str]:
    """The status columns THIS BU is decided by, and no others.

    TestRail custom fields are global, so a case in the TPS suite can carry a
    value in `Automation Status SD`.  Scanning every `status_*` column let one
    BU's field classify another BU's rows — a Perfume Shop case whose own field
    was empty came out Not Applicable because Superdrug's said so, and the
    export honestly reported that field as the one that decided it.
    """
    wanted = {f"status_{lbl}" for r in rules
              if (lbl := getattr(r, "status_field_label", None))}
    return [c for c in raw.columns if c in wanted]


def _is_to_update(series: pd.Series) -> pd.Series:
    """Boolean mask: status value equals a 'To be updated' label (normalised)."""
    return series.notna() & series.astype(str).str.strip().str.lower().isin(_STATUS_TO_UPDATE)

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


def _expand_baseline(raw: pd.DataFrame, rules: list,
                     *, member_label: str | None = None) -> pd.DataFrame:
    """Expand baseline cases into (case_id × country_label × device) rows.

    *member_label* selects the baseline.  None keeps the regression one exactly
    as it was: membership and devices both come from the big_regr labels.  Given
    a label (Production Sanity), membership comes from THAT label, while devices
    still come from the big_regr ones when the case has them — so a case in both
    baselines expands to the same rows in each, and "5 of those 100" is literally
    true.  A case carrying only the new label has no big_regr labels to read, so
    its devices come from the TestRail Device field instead.
    """
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

    # ── Filter to baseline (big_regr labels) ──────────────────────────────────
    # Device is TYPE-driven, matching the automated set (rules_engine):
    #   • an "API"-type case → a single "API" row (no desktop/mobile dimension);
    #   • any other type → Desktop/Mobile from the big_regr label(s), as before.
    # A case still needs a big_regr label to enter the baseline at all.
    _types = (raw["type_label"] if "type_label" in raw.columns
              else pd.Series([None] * len(raw), index=raw.index))

    _devs = (raw["device"] if "device" in raw.columns
             else pd.Series([None] * len(raw), index=raw.index))

    def _dev_for(labels, type_label, device_field) -> list[str]:
        if not isinstance(labels, list):
            return []
        if member_label is None:
            if not (_LABEL_DESKTOP in labels or _LABEL_MOBILE in labels):
                return []                              # not in the baseline
        elif member_label not in labels:
            return []
        if str(type_label).strip().upper() == "API":
            return ["API"]
        from_labels = ((["Desktop"] if _LABEL_DESKTOP in labels else []) +
                       (["Mobile"]  if _LABEL_MOBILE  in labels else []))
        if from_labels:
            return from_labels
        # Only reachable for a case that carries `member_label` and no big_regr
        # label: the TestRail Device field is the only thing left that knows.
        dev = str(device_field or "").strip()
        if dev == "Both":
            return ["Desktop", "Mobile"]
        return [dev] if dev in ("Desktop", "Mobile") else ["Unspecified"]

    # Positional selection, then ONE copy of the survivors: copying the whole
    # frame and slicing it afterwards paid for every case in the BU and left the
    # masks below working over a fragmented index, which cost more than the
    # filter saved.
    label_devs = [_dev_for(ls, t, d)
                  for ls, t, d in zip(raw["labels"], _types, _devs)]
    keep = [i for i, d in enumerate(label_devs) if d]
    if not keep:
        return _empty
    raw = raw.iloc[keep].copy().reset_index(drop=True)
    raw["_label_devs"] = [label_devs[i] for i in keep]

    # ── Status classification, on the baseline ONLY ──────────────────────────
    # Deliberately AFTER the membership filter.  It used to run over every case
    # in the BU and then throw most of the work away: measured at 48ms for a
    # baseline that produced zero rows, and 85ms where one case in four
    # qualified.  Across 11 BUs and two baselines that is over a second of a
    # cold start spent on cases nobody counts.
    #
    # A combined mask first, so non-TestIM rules (Java, etc.) reading a single
    # status field still classify correctly; TestIM cases are re-classified per
    # device after expansion (see below).
    status_cols = _read_status_cols(raw, rules)
    na_mask      = pd.Series(False, index=raw.index)
    tbu_mask     = pd.Series(False, index=raw.index)
    backlog_mask = pd.Series(False, index=raw.index)
    for col in status_cols:
        s      = raw[col]
        is_tbu = _is_to_update(s)
        na_mask      |= s.isin(_STATUS_NA)
        tbu_mask     |= is_tbu
        # Backlog excludes the auto / N/A / to-be-updated values.
        backlog_mask |= s.notna() & ~s.isin(_STATUS_AUTO | _STATUS_NA) & (s != "") & ~is_tbu

    raw["_cat_base"] = "unknown"
    raw.loc[backlog_mask,  "_cat_base"] = "backlog"
    raw.loc[tbu_mask,       "_cat_base"] = "to_be_updated"
    raw.loc[na_mask,        "_cat_base"] = "not_applicable"

    # ── Country expansion ─────────────────────────────────────────────────────
    if all_tokens:
        # Conditional tokens (e.g. ICI's LU only for Highest priority) are
        # dropped per-row BEFORE the BU-token match — same rule as the
        # automated set, so baseline and automation stay consistent.
        prios = (raw["priority_label"] if "priority_label" in raw.columns
                 else pd.Series([None] * len(raw), index=raw.index))
        raw["_countries"] = [
            list({
                token_label[t]
                for t in filter_conditional_tokens(
                    mc if isinstance(mc, list) else [], prio)
                if t in all_tokens
            })
            for mc, prio in zip(raw[country_col], prios)
        ]
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
    for dev, scol in _DEVICE_STATUS_COL.items():
        # `status_cols` is already restricted to this BU's fields; going around
        # it here would let the device path reintroduce another BU's verdict.
        if scol not in status_cols:
            continue
        dev_mask = raw["device_exp"] == dev
        if not dev_mask.any():
            continue
        col_vals = raw[scol]
        has_value = col_vals.notna() & (col_vals != "")
        is_tbu    = _is_to_update(col_vals)
        # Only reclassify rows where the device-specific TestIM field has a value.
        raw.loc[dev_mask & has_value & col_vals.isin(_STATUS_NA), "_cat_base"] = "not_applicable"
        raw.loc[
            dev_mask
            & has_value
            & ~col_vals.isin(_STATUS_AUTO | _STATUS_NA)
            & ~is_tbu,
            "_cat_base",
        ] = "backlog"
        raw.loc[dev_mask & is_tbu, "_cat_base"] = "to_be_updated"

    # ── Dedup on (case_id, country_label, device) ─────────────────────────────
    return (
        raw[["case_id", "country_label", "device_exp", "_cat_base"]]
        .drop_duplicates(subset=["case_id", "country_label", "device_exp"])
        .rename(columns={"device_exp": "device"})
        .reset_index(drop=True)
    )


def _expand_mapp_baseline(raw: pd.DataFrame, rules: list) -> pd.DataFrame:
    """Mobile-App baseline expansion.

    Different from the website baseline:
      * membership = Priority in {High, Highest} (no big_regr label);
      * device     = mobile OS (iOS / Android; "Both" already pre-split into the
                     `mapp_devices` list by rules_engine);
      * status     = the standard "Automation Status" field;
      * no country dimension — country_label is the BU name, matching the
        automated set (rules_engine emits country_label = rule.bu for MAPP).
    """
    _empty = pd.DataFrame(columns=["case_id", "country_label", "device", "_cat_base"])
    if raw.empty or "priority_label" not in raw.columns or "mapp_devices" not in raw.columns:
        return _empty

    bu = rules[0].bu if rules else "Mobile App"

    prio = raw["priority_label"].astype(str).str.strip().str.lower()
    raw = raw[prio.isin(_MAPP_PRIORITIES)]
    if raw.empty:
        return _empty

    # Status classification — same masks as the website baseline; for MAPP only
    # "Automation Status" is populated, so they reduce to that single field.
    status_cols = _read_status_cols(raw, rules)
    na_mask      = pd.Series(False, index=raw.index)
    tbu_mask     = pd.Series(False, index=raw.index)
    backlog_mask = pd.Series(False, index=raw.index)
    for col in status_cols:
        s      = raw[col]
        is_tbu = _is_to_update(s)
        na_mask      |= s.isin(_STATUS_NA)
        tbu_mask     |= is_tbu
        backlog_mask |= s.notna() & ~s.isin(_STATUS_AUTO | _STATUS_NA) & (s != "") & ~is_tbu

    raw = raw.copy()
    raw["_cat_base"] = "unknown"
    raw.loc[backlog_mask, "_cat_base"] = "backlog"
    raw.loc[tbu_mask,      "_cat_base"] = "to_be_updated"
    raw.loc[na_mask,       "_cat_base"] = "not_applicable"

    # Device expansion from the OS list (iOS / Android).
    raw["_devs"] = raw["mapp_devices"].apply(lambda d: d if isinstance(d, list) else [])
    raw = raw[raw["_devs"].map(len) > 0]
    if raw.empty:
        return _empty
    raw = raw.explode("_devs").rename(columns={"_devs": "device_exp"})
    raw["country_label"] = bu

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
    is_auto = merged["_auto"].fillna(False).to_numpy()

    # ── "To be updated" beats "Automated" ────────────────────────────────────
    # The two answer different questions and are written by different people:
    # the tool fields (Testim, KV SPR, MRN SPR, Playwright…) say whether a script
    # exists, and the manual QAs write "To be updated" when the test itself has
    # changed.  A script that no longer matches its test is work to do, not
    # coverage — so the flag wins, whichever field carries it.
    #
    # The row is still REMEMBERED as automated (`_automated_row`) so To Update
    # can say how much of it is maintenance of an existing script rather than
    # automation to write from scratch.
    expanded["_automated_row"] = is_auto
    expanded.loc[is_auto & (expanded["_cat_base"] != "to_be_updated"),
                 "category"] = "automated"

    # ── Rows of a case that IS automated somewhere ───────────────────────────
    # A test automated for NL but not BE used to land in the backlog with the
    # same weight as a test nobody has ever automated — and the QA leads read
    # those as two different jobs: extend an existing script vs write a new one.
    # "Backlog" therefore means "no automation anywhere"; the rest becomes
    # "partially_automated".  Both stay OUT of Automated, so Coverage is
    # untouched, and Total still equals the sum of the categories.
    #
    # UNKNOWN rows of such a case belong here too.  The status field is
    # case-level while country coverage is per-country, so when a case is
    # automated in 3 of its 5 countries the other 2 have no status of their own
    # to read: the case-level field says "Automated", which is not a backlog
    # value, and the row falls through to unknown.  Those rows are not a
    # mystery — the case is automated, just not there — which is exactly what
    # Partially Automated means.  Unknown is left to mean what it should: the
    # case is automated NOWHERE and no status explains why.
    auto_cases = set(expanded.loc[expanded["category"] == "automated", "case_id"])
    if auto_cases:
        expanded.loc[
            expanded["category"].isin(["backlog", "unknown"])
            & expanded["case_id"].isin(auto_cases),
            "category",
        ] = "partially_automated"
    return expanded


# ── stats ─────────────────────────────────────────────────────────────────────
def _stats(expanded: pd.DataFrame, auto: pd.DataFrame) -> dict:
    """Expanded row counts, unique case counts, and framework breakdown."""
    cats   = expanded["category"].value_counts()
    n_auto = int(cats.get("automated",      0))
    n_back = int(cats.get("backlog",         0))
    n_part = int(cats.get("partially_automated", 0))
    n_tbu  = int(cats.get("to_be_updated",   0))
    # Of those, the ones a script already covers: maintenance, not new work.
    tbu_auto = 0
    if "_automated_row" in expanded.columns:
        tbu_auto = int((expanded["category"].eq("to_be_updated")
                        & expanded["_automated_row"].fillna(False)).sum())
    n_na   = int(cats.get("not_applicable",  0))
    n_unk  = int(cats.get("unknown",         0))
    total  = len(expanded)

    def _u(cat: str) -> int:
        return int(expanded[expanded["category"] == cat]["case_id"].nunique())

    u_total = int(expanded["case_id"].nunique())
    u_auto  = _u("automated")
    u_back  = _u("backlog")
    u_tbu   = _u("to_be_updated")
    u_na    = _u("not_applicable")

    # Framework breakdown via merge (vectorised — no row-by-row apply).
    # `rules_engine._apply_framework_precedence` has already given each row ONE
    # framework (newest tool wins), so these three no longer overlap.
    counts_fw = {"java": (0, 0), "testim": (0, 0), "playwright": (0, 0)}
    if not auto.empty and n_auto > 0:
        auto_exp = expanded[expanded["category"] == "automated"].copy()
        auto_exp["case_id"] = auto_exp["case_id"].astype(int)

        base = auto[["case_id", "country_label", "device", "framework"]].copy()
        base["case_id"] = base["case_id"].astype(int)

        for fw_name, fw_mask in [
            ("java",       base["framework"] == "java"),
            ("testim",     base["framework"].isin(["testim_desktop", "testim_mobile"])),
            ("playwright", base["framework"] == "playwright"),
        ]:
            keys = (
                base[fw_mask][["case_id", "country_label", "device"]]
                .drop_duplicates()
                .assign(**{f"_{fw_name}": True})
            )
            m    = auto_exp.merge(keys, on=["case_id", "country_label", "device"], how="left")
            flag = f"_{fw_name}"
            matched = m[m[flag].fillna(False)]
            counts_fw[fw_name] = (int(m[flag].sum()),
                                  int(matched["case_id"].nunique()))
    n_java, u_java             = counts_fw["java"]
    n_testim, u_testim         = counts_fw["testim"]
    n_playwright, u_playwright = counts_fw["playwright"]

    # "To be updated" stays inside the automatable/scoped denominators (it was
    # previously part of Backlog), so Coverage % and N/A % are unchanged.
    # `automatable` keeps its meaning — everything that could still be
    # automated — so splitting the backlog leaves both ratios untouched.
    automatable = n_auto + n_back + n_part + n_tbu
    scoped      = n_auto + n_back + n_part + n_tbu + n_na
    ex_partial  = total - n_part

    # Mobile-App breakdown: MAPP has no Java/Testim — its meaningful split is the
    # OS, which the baseline rows already carry as the device.
    autos    = expanded[expanded["category"] == "automated"]
    n_ios     = int((autos["device"] == "iOS").sum())
    n_android = int((autos["device"] == "Android").sum())

    return {
        "total":           total,    "u_total":   u_total,
        "automated":       n_auto,   "u_auto":    u_auto,
        "java":            n_java,   "u_java":    u_java,
        "testim":          n_testim, "u_testim":  u_testim,
        "playwright":      n_playwright, "u_playwright": u_playwright,
        "ios":             n_ios,    "android":   n_android,
        "backlog":         n_back,   "u_back":    u_back,
        "partially_automated": n_part, "u_part":  _u("partially_automated"),
        "to_be_updated":   n_tbu,    "u_tbu":     u_tbu,
        "tbu_automated":   tbu_auto,
        "not_applicable":  n_na,     "u_na":      u_na,
        "unknown":         n_unk,
        "cov_total":       n_auto / total        * 100 if total        else 0.0,
        "cov_automatable": n_auto / automatable  * 100 if automatable  else 0.0,
        # Coverage with the partial gaps taken out of the baseline: a test
        # automated for NL but not BE is not held against the BU for BE.  The
        # Backlog still counts in full — a test nobody ever automated is real
        # missing coverage, not a gap in an existing script.
        "cov_ex_partial":  n_auto / ex_partial   * 100 if ex_partial   else 0.0,
        "na_pct":          n_na   / scoped        * 100 if scoped       else 0.0,
    }


# ── summary table ─────────────────────────────────────────────────────────────
# The three flavours of outstanding work, stacked into one column.  Order is
# deliberate: never automated, then automated-but-stale, then automated
# elsewhere — increasing proximity to being covered.
_STACK_COLS = ["Backlog", "To be Updated", "Partially Automated"]
_STACK_LABELS = {"Partially Automated": "Partially"}

_AUTO_SLIM_COLS = ["case_id", "country_label", "device", "framework"]


_SCOPE_DISPLAY = {"next_gen": "Microservices", "mobile_app": "Mobile App"}


def _build_summary(
    scope_data: dict[str, tuple],
    pairs: list[tuple[str, str]],
    member_label: str | None = None,
) -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame],
           dict[tuple[str, str], pd.DataFrame]]:
    """Build the summary table plus the per-(BU, scope) frames the detail view needs.

    *scope_data* maps scope → (raw, auto, rules) already filtered to that scope.
    *pairs* is the list of (bu, scope) to include.  Mobile-App scope uses the
    priority-based `_expand_mapp_baseline`; every other scope uses the label-
    based `_expand_baseline`.

    Returns (summary, expanded_by_bu, auto_by_bu):
      - expanded_by_bu: classified (case × country × device) baseline rows per BU
      - auto_by_bu:     the BU's automated rows slimmed to the 4 columns `_stats`
                        uses — kept small so the cached payload copies fast.
    """
    expanded_by_bu: dict[tuple[str, str], pd.DataFrame] = {}
    auto_by_bu:     dict[tuple[str, str], pd.DataFrame] = {}

    rows = []
    for bu, scope in pairs:
        if scope not in scope_data:
            continue
        raw_all, auto_all, rules_all = scope_data[scope]
        raw, auto, rules = _filter_bu(raw_all, auto_all, rules_all, bu)
        if raw.empty:
            continue
        expanded = (_expand_mapp_baseline(raw, rules) if scope == "mobile_app"
                    else _expand_baseline(raw, rules, member_label=member_label))
        if expanded.empty:
            continue
        expanded = _classify_expanded(expanded, auto)
        expanded_by_bu[(bu, scope)] = expanded
        auto_by_bu[(bu, scope)] = (
            auto[_AUTO_SLIM_COLS].copy() if not auto.empty
            else pd.DataFrame(columns=_AUTO_SLIM_COLS)
        )
        s = _stats(expanded, auto)
        # Framework columns are scope-specific: Java/Testim are website concepts,
        # so for Mobile App we show the OS split instead of two always-zero
        # columns (a column that always reads 0 is a label that lies).
        breakdown = ({"iOS": s["ios"], "Android": s["android"]}
                     if scope == "mobile_app"
                     else {"Java": s["java"], "TestIM": s["testim"],
                           "Playwright": s["playwright"]})
        rows.append({
            "BU":        bu,
            "Scope":     _SCOPE_DISPLAY.get(scope, "Website"),
            "Total":     s["total"],
            "Automated": s["automated"],
            **breakdown,
            "Backlog":   s["backlog"],
            "Partially Automated": s["partially_automated"],
            "To be Updated": s["to_be_updated"],
            "Not Applicable": s["not_applicable"],
            "Unknown":   s["unknown"],
            "Coverage %":    round(s["cov_total"], 1),
            "Coverage excl. Partially %": round(s["cov_ex_partial"], 1),
        })
    return pd.DataFrame(rows), expanded_by_bu, auto_by_bu


@st.cache_data(ttl=21600, show_spinner=False)
def _backlog_data() -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame],
                             dict[tuple[str, str], pd.DataFrame]]:
    """The heavy 11-BU baseline pipeline (expand + classify + stats), computed
    ONCE per data refresh (TTL matches `evaluate_rules`) and shared by the
    Backlog tab (summary + detail view) and Dexter's coverage brief.

    Without this cache the whole pandas pipeline re-ran on EVERY widget
    interaction inside the fragment (BU selectbox, pivot multiselects) — the
    main source of Backlog-tab interaction lag."""
    scope_data: dict[str, tuple] = {}
    for scope in ("website", "next_gen"):
        raw, auto, rules = _load_scope(scope)
        if not raw.empty:
            scope_data[scope] = (raw, auto, rules)
    return _build_summary(scope_data, _scoped_bus())


def _carries_label(raw: pd.DataFrame, label: str) -> bool:
    """Does any case in *raw* carry *label*?  One vectorised pass over a frame
    that is already in memory — cheap enough to gate a whole pipeline on."""
    if raw.empty or "labels" not in raw.columns:
        return False
    return bool(raw["labels"].map(
        lambda ls: isinstance(ls, list) and label in ls).any())


# ── runs ─────────────────────────────────────────────────────────────────────
# The three populations are RUNS — which is how the spreadsheet this dashboard
# replaces already names them ("Full regression run", "Release regression run",
# "Prod Sanity run").  One control picks one, and the whole tab reports on it:
# summary table, tiles, coverage, frameworks and pivot.  Stacking them instead
# meant a reader met three sets of "Total / Automated / Backlog" on one page,
# and the page grew a section every time the business gained a run.
RUN_BIG   = "Big No-Regression"
RUN_SMALL = "Small No-Regression"
RUN_PS    = "Production Sanity"
RUNS = [RUN_BIG, RUN_SMALL, RUN_PS]

# One sentence per run, shown beside the picker.  It fills the space the control
# leaves and earns it: switching run changes every number on the page, and what
# each one MEANS had nowhere else to live once the tile tooltips went.
_RUN_MEANING = {
    RUN_BIG:   "Every case labelled `big_regr_desktop` or `big_regr_mobile`.",
    RUN_SMALL: "The subset also ticked `small_nr` — the Release run.",
    RUN_PS:    "Cases labelled `prod_sanity`, counted separately from the "
               "regression baseline.",
}


def _small_nr_cases(scope: str) -> set[int]:
    """Case IDs carrying the `small_nr` checkbox.

    A SUBSET marker: every case carrying it is already in the big_regr
    baseline, so this never adds rows — it only narrows the ones already there.
    """
    try:
        raw, _auto, _rules = _load_scope(scope)
    except Exception:                                                   # noqa: BLE001
        return set()
    if raw.empty or "small_nr" not in raw.columns:
        return set()
    return set(raw.loc[raw["small_nr"].fillna(False), "case_id"].astype(int))


def _run_data(run: str, scope: str):
    """(summary, expanded_by_bu, auto_by_bu) for the selected run.

    Small NR filters the regression payload instead of expanding a second time:
    the subset shares every row with the baseline, so a second expansion could
    only produce the same rows more slowly — or, worse, differently.
    """
    if run == RUN_PS:
        return _prod_sanity_data()
    loader = _mapp_backlog_data if scope == "mobile_app" else _backlog_data
    summary, expanded_by_bu, auto_by_bu = loader()
    if run != RUN_SMALL:
        return summary, expanded_by_bu, auto_by_bu

    ids = _small_nr_cases(scope)
    if not ids:
        return pd.DataFrame(), {}, {}
    small = {k: e[e["case_id"].astype(int).isin(ids)]
             for k, e in expanded_by_bu.items()}
    rows = []
    for (bu, sc), exp in small.items():
        if exp.empty:
            continue
        st_ = _stats(exp, auto_by_bu.get((bu, sc),
                                         pd.DataFrame(columns=_AUTO_SLIM_COLS)))
        rows.append({
            "BU": bu, "Scope": _SCOPE_DISPLAY.get(sc, "Website"),
            "Total": st_["total"], "Automated": st_["automated"],
            "Java": st_["java"], "TestIM": st_["testim"],
            "Playwright": st_["playwright"], "Backlog": st_["backlog"],
            "Partially Automated": st_["partially_automated"],
            "To be Updated": st_["to_be_updated"],
            "Not Applicable": st_["not_applicable"], "Unknown": st_["unknown"],
            "Coverage %": round(st_["cov_total"], 1),
            "Coverage excl. Partially %": round(st_["cov_ex_partial"], 1),
        })
    return pd.DataFrame(rows), small, auto_by_bu


@st.cache_data(ttl=21600, show_spinner=False)
def _prod_sanity_data() -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame],
                                 dict[tuple[str, str], pd.DataFrame]]:
    """The Production Sanity baseline, kept SEPARATE from `_backlog_data`.

    An independent population that may overlap the regression one: a case with
    both labels is counted in both, so nothing here can move a regression
    number.  Returns empty frames until a case actually carries the label,
    which is what lets every Production Sanity surface hide itself.
    """
    scope_data: dict[str, tuple] = {}
    for scope in ("website", "next_gen"):
        raw, auto, rules = _load_scope(scope)
        if not raw.empty and _carries_label(raw, _LABEL_PROD_SANITY):
            scope_data[scope] = (raw, auto, rules)
    if not scope_data:
        # No case carries the label yet: skip the whole pipeline rather than run
        # an 11-BU expansion that can only produce empty frames.
        return pd.DataFrame(), {}, {}
    return _build_summary(scope_data, _scoped_bus(),
                          member_label=_LABEL_PROD_SANITY)


@st.cache_data(ttl=21600, show_spinner=False)
def _mapp_backlog_data() -> tuple[pd.DataFrame, dict[tuple[str, str], pd.DataFrame],
                                  dict[tuple[str, str], pd.DataFrame]]:
    """The Mobile-App baseline pipeline, kept SEPARATE from `_backlog_data` so the
    website/microservices numbers (KPI strip, All-BU table, Report, Dexter) stay
    untouched.  Loaded lazily — mobile_app is deferred from the start-up warm-up,
    so this only fetches when the user actually opens the Mobile App scope."""
    raw, auto, rules = _load_scope("mobile_app")
    if raw.empty:
        return pd.DataFrame(), {}, {}
    return _build_summary(
        {"mobile_app": (raw, auto, rules)},
        [(bu, "mobile_app") for bu in MOBILE_APP_BUS],
    )


# ── detail view ───────────────────────────────────────────────────────────────
# Backlog (pure backlog rows) is "healthy" while it stays under this share of the
# baseline Total.  Under → green ▼, over → red ▲.
_BACKLOG_THRESHOLD_PCT = 3.0


def _backlog_badge_html(backlog: int, total: int) -> str:
    """Inline green/red health pill: Backlog as a % of the baseline Total."""
    pct   = (backlog / total * 100) if total else 0.0
    over  = pct > _BACKLOG_THRESHOLD_PCT
    color = COLORS["danger"] if over else COLORS["success"]
    bg    = "#FCE7E7" if over else "#E6F6EC"
    arrow = "▲" if over else "▼"
    return (
        f"<span style='display:inline-flex;align-items:center;gap:4px;padding:3px 9px;"
        f"border-radius:999px;background:{bg};color:{color};font-size:11px;font-weight:700;"
        f"line-height:1' title='Backlog is {pct:.1f}% of the baseline Total "
        f"(threshold {_BACKLOG_THRESHOLD_PCT:.0f}%).'>{arrow} {pct:.1f}%</span>"
    )


def _framework_cards(s: dict) -> list[tuple[str, int, int]]:
    """(label, rows, cases) for the frameworks that actually carry rows.

    A card reading 0 is a label that lies: Watsons is 100% TestIM, so a "Java 0"
    card next to it was pure noise, and the same holds for Playwright until the
    migration produces its first labelled case.  Mobile App, whose rows carry the
    `mobile_app` framework, gets an empty list — the caller then drops the whole
    section instead of showing a row of zeros.
    """
    return [(name, n, u) for name, n, u in (
        ("Java",       s["java"],       s["u_java"]),
        ("TestIM",     s["testim"],     s["u_testim"]),
        ("Playwright", s["playwright"], s["u_playwright"]),
    ) if n]


def _stat_card(col, label: str, n: int, u: int, *,
               badge_html: str = "") -> None:
    """Thin alias — the card itself lives in styles.stat_card, shared with the
    Report tab so the two never drift apart."""
    stat_card(col, label, n, u, badge_html=badge_html)


def _baseline_pivot(expanded: pd.DataFrame, key_prefix: str) -> None:
    """Interactive pivot over the full regression baseline (all categories)."""
    section_title("Pivot")
    if expanded.empty:
        return

    # Build display DataFrame
    disp = expanded[["case_id", "country_label", "device", "category"]].copy()
    disp["Country"]  = disp["country_label"].map(lambda c: COUNTRY_NAMES.get(c, c))
    disp["Device"]   = disp["device"]
    disp["Category"] = disp["category"].map({
        "automated":      "Automated",
        "backlog":              "Backlog",
        "partially_automated":  "Partially Automated",
        "to_be_updated":  "To be Updated",
        "not_applicable": "Not Applicable",
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
        st.dataframe(pv, width="stretch")
    except Exception as exc:
        st.error(f"Pivot error: {exc}")


# The six tiles that offer an export, as (category, label).  Nothing in the
# render reads this list — the tiles are built from `_stats` — so it is the
# CONTRACT the tests hold the tiles to: every entry here must be a category the
# classifier can produce and `_csv_writer` can serialise.  Add a tile, add it
# here, or the export tests stop covering it.
_EXPORT_CATEGORIES = [
    ("total",               "Total"),
    ("automated",           "Automated"),
    ("backlog",             "Backlog"),
    ("partially_automated", "Partially Automated"),
    ("to_be_updated",       "To be Updated"),
    ("not_applicable",      "Not Applicable"),
]


_CATEGORY_LABELS = {
    "total": "Total", "automated": "Automated", "backlog": "Backlog",
    "partially_automated": "Partially Automated", "to_be_updated": "To be Updated",
    "not_applicable": "Not Applicable", "unknown": "Unknown",
}


def _deciding_field(case: dict, device: str, category: str,
                    allowed: list[str] | None = None) -> tuple[str, str]:
    """Which TestRail field decided this row, and with what value.

    Mirrors `_expand_baseline`: a device row is judged by its own TestIM field
    when that field is populated, otherwise by whichever status field carries a
    verdict.  The answer must match the row's category — reporting "Automated
    UAT" next to a row that is NOT automated (because that automation covers a
    different country) would make the export lie about its own numbers.
    """
    if category == "partially_automated":
        return ("Country / device coverage",
                "Automated on another country or device, not on this one")
    if not case:
        return ("—", "—")

    def _val(col):
        v = case.get(col)
        return v.strip() if isinstance(v, str) and v.strip() else None

    def _fits(v: str) -> bool:
        if category == "automated":
            return v in _STATUS_AUTO
        if category == "not_applicable":
            return v in _STATUS_NA
        if category == "to_be_updated":
            return v.strip().lower() in _STATUS_TO_UPDATE
        if category == "backlog":
            return (v not in _STATUS_AUTO and v not in _STATUS_NA
                    and v.strip().lower() not in _STATUS_TO_UPDATE)
        return True                        # "total" export: any verdict will do

    # Only the fields this BU is decided by.  Naming any other one would
    # report a verdict that took no part in the classification — which is how a
    # Perfume Shop row came out "decided by Automation Status SD".
    pool = (allowed if allowed is not None
            else [c for c in case if c.startswith("status_")])
    dev_col = _DEVICE_STATUS_COL.get(device)
    dev_col = dev_col if dev_col in pool else None
    ordered = ([dev_col] if dev_col else []) + [c for c in pool if c != dev_col]
    # The device's own field first, then the rest — the classifier's order.
    for col in ordered:
        if col and (v := _val(col)) and _fits(v):
            return (col[len("status_"):], v)
    for col in ordered:                    # fall back to any populated field
        if col and (v := _val(col)):
            return (col[len("status_"):], v)
    return ("—", "not set")


def _evidence_frame(expanded: pd.DataFrame, scope: str,
                    bu: str = "") -> pd.DataFrame:
    """Every baseline row with the TestRail evidence behind its classification.

    Built ONCE per BU (see `_tile_evidence`): the per-case metadata join, the
    dict build and the deciding-field pass are the expensive part, and doing
    them per tile cost ~460ms on every rerun of the tab.
    """
    if expanded is None or expanded.empty:
        return pd.DataFrame()

    base_cols = ["case_id", "title", "url", "section_path", "priority_label",
                 "type_label", "device", "automation_tool", "labels",
                 "multi_countries", "country_coverage"]
    meta_by_case: dict[int, dict] = {}
    meta = pd.DataFrame(columns=["case_id"])
    try:
        raw, _auto, _rules = _load_scope(scope)     # cache hit, no TestRail call
        cols = ([c for c in base_cols if c in raw.columns]
                + [c for c in raw.columns if c.startswith("status_")])
        meta = raw[cols].drop_duplicates("case_id").copy()
        meta["case_id"] = meta["case_id"].astype(int)
        meta_by_case = meta.set_index("case_id").to_dict("index")
        # `device` here is the TestRail field, not the expanded row's device.
        meta = meta.rename(columns={"device": "device_field"})
    except Exception:                                                   # noqa: BLE001
        pass

    out = expanded[["case_id", "country_label", "device", "category"]].copy()
    out["case_id"] = out["case_id"].astype(int)
    out["_cat"] = out["category"]
    if not meta.empty:
        out = out.merge(meta, on="case_id", how="left")

    out.insert(0, "Case ID", "C" + out["case_id"].astype(str))
    allowed = ([f"status_{r.status_field_label}"
                for r in ALL_RULES if r.bu == bu and r.scope == scope]
               if bu else None)
    decided = [_deciding_field(meta_by_case.get(cid, {}), dev, cat, allowed)
               for cid, dev, cat in zip(out["case_id"], out["device"],
                                        out["_cat"])]
    out["category"]       = out["category"].map(_CATEGORY_LABELS).fillna(out["category"])
    out["Decided By"]     = [d[0] for d in decided]
    out["Deciding Value"] = [d[1] for d in decided]

    out = out.rename(columns={"title": "Title", "country_label": "Country",
                              "device": "Device", "category": "Category",
                              "section_path": "Section", "url": "TestRail Link",
                              "priority_label": "Priority", "type_label": "Type",
                              "automation_tool": "Automation Tool"})
    out = out.rename(columns={c: c[len("status_"):]
                              for c in out.columns if c.startswith("status_")})
    # Country fields carry the tokens of EVERY BU sharing the suite, so an ICI
    # row showed "IPXL NL, MRN, MFR" and read as if it counted for Marionnaud.
    # The counts never did — a case with no token of this BU is dropped before
    # expansion — but a column that says otherwise is worse than no column.
    # Split it: what counted for this BU, and what belongs to the others.
    bu_tokens = {t for r in ALL_RULES
                 if r.bu == bu and r.scope == scope for t in r.countries_filter}

    def _split(v) -> tuple[str, str]:
        toks = [str(t) for t in v] if isinstance(v, list) else []
        mine = [t for t in toks if t in bu_tokens]
        other = [t for t in toks if t not in bu_tokens]
        return ", ".join(mine), ", ".join(other)

    if "multi_countries" in out.columns and bu_tokens:
        split = [_split(v) for v in out["multi_countries"]]
        out["multi_countries"] = [m for m, _ in split]
        other = [o for _, o in split]
        if any(other):
            out["Other BUs on this case"] = other
    for col in ("labels", "multi_countries", "country_coverage"):
        if col in out.columns:
            out[col] = out[col].map(
                lambda v: ", ".join(v) if isinstance(v, list) else (v or ""))
    out = out.rename(columns={"labels": "Labels",
                              "multi_countries": "Countries counted for this BU",
                              "country_coverage": "Country Coverage",
                              "device_field": "Device (TestRail field)"})

    lead = ["Case ID", "Title", "Country", "Device", "Category",
            "Decided By", "Deciding Value"]
    tail = ["Section", "TestRail Link"]
    middle = [c for c in out.columns
              if c not in lead + tail + ["case_id", "_cat"]]
    keep = ([c for c in lead if c in out.columns] + sorted(middle)
            + [c for c in tail if c in out.columns])
    return out[keep + ["_cat"]].sort_values(
        ["Category", "Case ID", "Country", "Device"])


def _category_rows(expanded: pd.DataFrame, category: str,
                   scope: str) -> pd.DataFrame:
    """The rows behind one tile — the whole frame for "total"."""
    ev = _evidence_frame(expanded, scope)
    if ev.empty:
        return ev
    sub = ev if category == "total" else ev[ev["_cat"] == category]
    return sub.drop(columns=["_cat"])


@st.cache_data(ttl=21600, show_spinner=False)
def _tile_evidence(bu: str, scope: str,
                   baseline: str = "regression") -> pd.DataFrame:
    """The evidence behind a BU's tiles, built once per BU and refresh.

    Only the FRAME is cached.  Turning it into CSV bytes is left to the download
    button's deferred callable (see `_csv_writer`), so a category nobody clicks
    never costs a serialisation — and we stop holding six ready-made CSVs per
    BU in memory for six hours, which on a 5,800-row BU is megabytes of files
    that may never be downloaded.

    The frame build itself stays cached because it is the expensive half
    (~460ms with ICI-sized data: the per-case metadata join, the dict build and
    the deciding-field pass).  Keyed like the rest of the data caches, so ↻
    clears it alongside the numbers.
    """
    loader = _mapp_backlog_data if scope == "mobile_app" else _backlog_data
    try:
        _summary, expanded_by_bu, _auto_by_bu = loader()
    except Exception:                                                   # noqa: BLE001
        return pd.DataFrame()
    return _evidence_frame(expanded_by_bu.get((bu, scope)), scope, bu)


# What each category means in TestRail terms.  Written from the same constants
# the classifier uses, so the recipe cannot drift from the numbers it explains.
_STATUS_PREDICATE = {
    "total":               "any value, including empty",
    "automated":           "one of: {auto}",
    "backlog":             "set, and NOT one of: {auto} / {na} / To be updated",
    "partially_automated": "set, and NOT one of: {auto} / {na} / To be updated",
    "to_be_updated":       "To be updated",
    "not_applicable":      "{na}",
}

# The parts of a category TestRail's filters cannot express.  Naming them is the
# point: a lead who filters and gets a different count needs to know which of
# these explains the gap, not to be told the dashboard is right.
_NOT_EXPRESSIBLE = {
    "total": [],
    "automated": [
        "A row is automated per (case × country × device).  TestRail returns "
        "CASES, so a case automated for NL but not BE is returned whole here "
        "and counted once there.",
        "\"To be updated\" wins over Automated on this dashboard: a case whose "
        "test has changed is NOT counted as automated even where a script "
        "exists.  A TestRail filter on the automated values still returns it.",
    ],
    "backlog": [
        "Backlog here means the case is automated NOWHERE.  A case automated "
        "on another country or device is Partially Automated instead — "
        "TestRail cannot tell the two apart, so its filter returns both.",
    ],
    "partially_automated": [
        "This category exists only against the computed automated set: the "
        "case IS automated on some other country/device.  No TestRail filter "
        "can express it — use the Rows sheet to identify the cases.",
    ],
    "to_be_updated": [
        "The flag is read from ANY status field, and it beats Automated, so "
        "some of these rows also carry an automated value elsewhere.",
    ],
    "not_applicable": [],
}


def _filter_recipe(bu: str, scope: str, category: str,
                   n_rows: int, n_cases: int,
                   member_label: str | None = None) -> pd.DataFrame:
    """How to pull the same subset out of TestRail, generated from the rules.

    Reproducing a number by hand is how a QA lead checks it, and every time
    somebody has tried this week the two counts differed for a reason nobody
    could see.  The recipe states the filters AND what TestRail cannot express,
    so a mismatch points at its cause instead of at the dashboard.
    """
    rules = [r for r in ALL_RULES if r.bu == bu and r.scope == scope]
    if not rules:
        return pd.DataFrame()
    auto_vals = ", ".join(sorted({v for r in rules for v in r.automated_values}))
    na_vals   = ", ".join(sorted(_STATUS_NA))
    labels = (member_label if member_label
              else f"{_LABEL_DESKTOP} and/or {_LABEL_MOBILE}")

    rows: list[dict] = [
        {"Field": "Business Unit",  "Filter": bu},
        {"Field": "Suite ID",       "Filter": ", ".join(
            str(x) for x in sorted({r.suite_id for r in rules}))},
        {"Field": "Deprecated",     "Filter": "No"},
        {"Field": "Labels",         "Filter": f"contains {labels}"},
    ]
    types = sorted({t for r in rules for t in r.type_filter})
    rows.append({"Field": "Type",
                 "Filter": ", ".join(types) if types else "any"})

    pred = _STATUS_PREDICATE.get(category, "")
    for r in rules:
        gate = getattr(r, "labels_filter", [])
        rows.append({
            "Field": f"{r.status_field_label}  ({r.framework})",
            "Filter": pred.format(auto=auto_vals, na=na_vals)
                      # Without this the reader would filter on the status alone
                      # and pull in every legacy case sharing that field.
                      + (f"   AND label: {', '.join(gate)}" if gate else ""),
        })
        rows.append({
            "Field": f"{r.country_field_label}  (for {r.framework})",
            "Filter": "one of: " + ", ".join(r.countries_filter)
                      if r.countries_filter else "any",
        })
    if any("IPXL LU" in r.countries_filter for r in rules):
        rows.append({"Field": "Conditional country",
                     "Filter": "IPXL LU counts only on Priority = Highest"})

    try:
        base = tr.TestRailCredentials.from_secrets().base_url.rstrip("/")
        for sid in sorted({r.suite_id for r in rules}):
            rows.append({"Field": f"Open suite {sid}",
                         "Filter": f"{base}/index.php?/suites/view/{sid}"})
    except Exception:                                                   # noqa: BLE001
        pass

    rows += [
        {"Field": "", "Filter": ""},
        {"Field": "EXPECTED", "Filter": f"{n_rows:,} rows over {n_cases:,} cases"},
        {"Field": "Why they differ",
         "Filter": "the dashboard counts case × country × device; TestRail "
                   "counts cases"},
    ]
    for note in _NOT_EXPRESSIBLE.get(category, []):
        rows.append({"Field": "Not expressible in TestRail", "Filter": note})
    return pd.DataFrame(rows)


def _csv_writer(evidence: pd.DataFrame, category: str,
                bu: str = "", scope: str = "", member_label: str | None = None):
    """A deferred payload for `st.download_button`: Streamlit runs it only when
    the button is actually clicked (`data` accepts a callable since 1.36).

    Two sheets.  "Rows" is the data behind the number; "TestRail filters" is how
    to pull the same subset by hand, plus what TestRail cannot express — because
    the way anyone checks a number is by trying to reproduce it, and a bare list
    of rows does not survive that conversation.

    The frame is CAPTURED in the closure rather than looked up when the click
    arrives.  The callable runs outside the script run, where a `st.cache_data`
    lookup is not guaranteed to hit — and a miss there would re-run the whole
    11-BU pipeline as the response to a download click.
    """
    def _build() -> bytes:
        sub = (evidence if category == "total"
               else evidence[evidence["_cat"] == category])
        sub = sub.drop(columns=["_cat"])
        n_cases = (sub["Case ID"].nunique() if "Case ID" in sub.columns
                   else len(sub))
        recipe = _filter_recipe(bu, scope, category, len(sub), n_cases,
                                member_label)
        if recipe.empty:
            return sub.to_csv(index=False).encode("utf-8")
        try:
            import io
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xl:
                sub.to_excel(xl, sheet_name="Rows", index=False)
                recipe.to_excel(xl, sheet_name="TestRail filters", index=False)
            return buf.getvalue()
        except Exception:                                               # noqa: BLE001
            # No Excel engine on the host: the rows are the point, the format
            # is not worth an exception in front of someone who just clicked.
            return sub.to_csv(index=False).encode("utf-8")
    return _build


def _detail_view(
    bu: str,
    scope: str,
    expanded_by_bu: dict[tuple[str, str], pd.DataFrame],
    auto_by_bu: dict[tuple[str, str], pd.DataFrame],
    run: str = RUN_BIG,
) -> None:
    # Both frames come straight from the cached `_backlog_data()` payload — no
    # recomputation on widget interactions.
    expanded = expanded_by_bu.get((bu, scope))
    if expanded is None or expanded.empty:
        st.info(
            "No big_regr cases found for this BU. "
            "Check that cases have the 'big_regr_desktop' / 'big_regr_mobile' "
            "label — new labels appear at the next data refresh (↻ next to the tabs)."
        )
        return
    auto = auto_by_bu.get((bu, scope))
    if auto is None:
        auto = pd.DataFrame(columns=_AUTO_SLIM_COLS)

    s = _stats(expanded, auto)

    # ── Row 1: Total · Automated · Backlog · To update · N/A ─────────────────
    # Each tile is its own container so the download button can be stretched
    # over it (see `.st-key-tile_` in styles.py): clicking the number gives you
    # the rows behind it, with no second control on screen.
    # No help text: the label and the number say it, and an ⓘ on every tile was
    # six tooltips nobody opened.  What the categories mean lives in "How
    # numbers are calculated", once, where it can be read in context.
    tiles = [
        ("total", "Total", s["total"], s["u_total"], ""),
        ("automated", "Automated", s["automated"], s["u_auto"], ""),
        ("backlog", "Backlog", s["backlog"], s["u_back"],
         _backlog_badge_html(s["backlog"], s["total"])),
        ("partially_automated", "Partially Automated",
         s["partially_automated"], s["u_part"], ""),
        ("to_be_updated", "To be Updated", s["to_be_updated"], s["u_tbu"], ""),
        ("not_applicable", "Not Applicable", s["not_applicable"], s["u_na"], ""),
    ]
    key_base = re.sub(r"[^A-Za-z0-9]+", "_",
                      f"{scope}_{bu}_{RUNS.index(run) if run in RUNS else 0}")
    evidence = _tile_evidence(bu, scope,
                              baseline=("prod_sanity" if run == RUN_PS
                                        else "regression"))     # cached: one build per refresh
    for col, (cat, label, n, u, badge) in zip(st.columns(6), tiles):
        with col.container(key=f"tile_{key_base}_{cat}"):
            stat_card(st, label, n, u, badge_html=badge)
            # The row count comes from the tile's own number instead of being
            # counted back out of the CSV — the file no longer exists at render
            # time, and the two were always the same figure anyway.
            if evidence.empty or not n:
                continue
            st.download_button(
                f"Download the {n:,} rows behind {label}",
                _csv_writer(evidence, cat, bu, scope),
                file_name=f"{bu.replace(' ', '_')}_{label.replace(' ', '_')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument."
                     "spreadsheetml.sheet",
                # No `help`: the button is stretched invisibly over the whole
                # tile, so its tooltip followed the cursor across the card.
                key=f"dl_{key_base}_{cat}",
            )


    # ── Coverage line (with N/A %) ────────────────────────────────────────────
    # The third figure appears only where there ARE partial gaps — everywhere
    # else it repeats Coverage to the decimal, and a duplicated number reads as
    # a broken one.
    cov_parts = [
        f"**Coverage:** `{s['cov_total']:.1f}%`",
        f"**Coverage vs Automatable:** `{s['cov_automatable']:.1f}%`",
    ]
    if s["partially_automated"]:
        cov_parts.append("**Coverage excluding Partially Automated:** "
                         f"`{s['cov_ex_partial']:.1f}%`")
    cov_parts.append(f"**Not Applicable:** `{s['na_pct']:.1f}%`")
    st.markdown(" &nbsp;·&nbsp; ".join(cov_parts), unsafe_allow_html=True)

    st.divider()

    # ── Row 2: Framework breakdown ────────────────────────────────────────────
    frameworks = _framework_cards(s)
    if frameworks:
        section_title("Automated by Framework")
        # With a single framework the shares would read "100%", which the card
        # above already says — the panel earns its space only when there is a
        # split to explain.  Column count stays at 3 so the cards keep the same
        # width from one BU to the next.
        show_shares = len(frameworks) > 1
        cols = st.columns(max(3, len(frameworks) + (1 if show_shares else 0)))
        for col, (name, n, u) in zip(cols, frameworks):
            _stat_card(col, name, n, u)

        if show_shares:
            # `<br>` as a SEPARATOR, not a suffix: the caption that used to sit
            # under the last percentage is gone, so a trailing break would leave
            # a blank line hanging off the panel.
            shares = "<br>".join(
                f"{name} &nbsp;<b>"
                f"{(n / s['automated'] * 100 if s['automated'] else 0.0):.1f}%</b>"
                for name, n, _ in frameworks
            )
            cols[len(frameworks)].markdown(
                f"<div style='padding-top:8px;font-size:13px;color:{COLORS['text']}'>"
                f"{shares}</div>",
                unsafe_allow_html=True,
            )
        st.divider()

    # ── Pivot ─────────────────────────────────────────────────────────────────
    bu_key  = bu.lower().replace(" ", "_")
    run_key = RUNS.index(run) if run in RUNS else 0
    # The pivot key carries the run: without it the three would share one
    # widget state and a Country/Device choice made on one would silently
    # reappear on another, over different rows.
    _baseline_pivot(expanded, key_prefix=f"bl_{bu_key}_{scope}_{run_key}")



# ── render ────────────────────────────────────────────────────────────────────
def _backlog_pct_html(backlog: int, total: int) -> str:
    """The same health verdict as the card's pill, quiet enough for a table row.

    The pill's tinted background repeated on eight rows would fight the coverage
    bars for attention, so here it is just the percentage in the verdict colour.
    """
    if not total:
        return ""
    pct   = backlog / total * 100
    over  = pct > _BACKLOG_THRESHOLD_PCT
    color = COLORS["danger"] if over else COLORS["success"]
    return (
        f"<span class='bl-pct' style='color:{color}' "
        f"title='Backlog is {pct:.1f}% of the baseline Total "
        f"(healthy \u2264 {_BACKLOG_THRESHOLD_PCT:.0f}%).'>{pct:.1f}%</span>"
    )


def _summary_table_html(df: pd.DataFrame, num_cols: list[str],
                        selected_bu: str = "",
                        backlog_health: bool = True) -> str:
    """Presentation-grade HTML for the All-BU summary — same data as the native
    dataframe, with an RAG coverage bar and tidy typography.  Styling lives in
    the `.bl-summary` CSS block in styles.py.

    *selected_bu* marks the row the global filter is on, so the reader can find
    "their" BU in an eight-row table without counting down the rows.

    *backlog_health* draws the health percentage under the Backlog number.  Off
    for Production Sanity: the 3% threshold was agreed for the regression
    baseline, and a verdict borrowed from another population is a verdict that
    has not been agreed at all.
    """
    strong_cols = {"Total", "Automated", "Backlog"}   # numbers a manager reads first
    # Three columns of outstanding work, stacked into one labelled cell.  They
    # answer the same question — what is not covered by a working script — and
    # three headers wide enough to hold "PARTIALLY AUTOMATED" cost more width
    # than the numbers inside them ever needed.
    stack_cols = [c for c in _STACK_COLS if c in num_cols]
    items: list[tuple[str, object]] = []
    for c in num_cols:
        if c not in stack_cols:
            items.append(("col", c))
        elif not any(k == "stack" for k, _ in items):
            items.append(("stack", stack_cols))
    # The scope radio above the tabs already filters the table to one scope, so
    # the column repeats the same pill on every row — a whole column of no
    # information, and the reason the table needed a horizontal scrollbar.  It
    # comes back if a view ever does mix scopes (the safety-net fallback).
    show_scope = df["Scope"].nunique() > 1 if "Scope" in df.columns else False
    # The column is named after the figure it actually leads with.  Where no row
    # has partial gaps — Production Sanity has no such category at all — the two
    # are the same number and the qualifier would describe a distinction that is
    # not on screen.
    leads_ex = (
        "Coverage excl. Partially %" in df.columns
        and bool((df["Coverage excl. Partially %"].round(1)
                  != df["Coverage %"].round(1)).any())
    )
    cov_head = "Coverage excl. Partially" if leads_ex else "Coverage"
    head = (
        '<thead><tr>'
        '<th class="l">Business Unit</th>'
        + ('<th class="l">Scope</th>' if show_scope else '')
        + "".join(f'<th>{v}</th>' if k == "col" else '<th>Outstanding</th>'
                   for k, v in items)
        + f'<th class="l">{cov_head}</th></tr></thead>'
    )
    body_rows = []
    for _, r in df.iterrows():
        # The bar, the colour and the big figure are on coverage EXCLUDING the
        # partial gaps: the question this table is read for is "how are we doing
        # on the tests we have started".  The coverage over the whole baseline —
        # the figure the KPI strip, the Coverage tab, the Report and Dexter all
        # show — stays underneath in grey, so the two can never be confused and
        # the table still reconciles with the rest of the app.
        real = float(r["Coverage %"])
        cov  = float(r.get("Coverage excl. Partially %", real) or real)
        _dot, color = coverage_health(cov)
        def _plain(col: str) -> str:
            return (f'<td class="{"strong" if col in strong_cols else "mut"}">'
                    f'{int(r[col]):,}'
                    + (_backlog_pct_html(int(r[col]), int(r["Total"]))
                       if col == "Backlog" and backlog_health else "")
                    + '</td>')

        def _stacked(cols: list[str]) -> str:
            # The health verdict rides with the LABEL, not after the figure: it
            # qualifies the backlog, and putting it right of the number pushed
            # the value column off the cell's right edge, so the figures no
            # longer sat under their own header.
            # Three fixed-width grid cells per line: health · label · figure.
            # Sharing a cell with the label let the health verdict widen that
            # column on the Backlog line only, and since the block is anchored
            # right, every row with a wider figure or a wider verdict shifted
            # sideways.  Fixed columns make the three lines — and all nine rows —
            # line up with each other.
            lines = ""
            for c in cols:
                health = (_backlog_pct_html(int(r[c]), int(r["Total"]))
                          if c == "Backlog" and backlog_health else "")
                lines += (f"<u>{health}</u>"
                          f"<i>{_STACK_LABELS.get(c, c)}</i>"
                          f"<b>{int(r[c]):,}</b>")
            # No "l" class: the cell keeps the table's right alignment, so the
            # figures line up under the header and with every other column.
            return f'<td><span class="stack">{lines}</span></td>'

        nums = "".join(_plain(v) if k == "col" else _stacked(v)  # type: ignore[arg-type]
                       for k, v in items)
        # Only where the two differ: with no partial rows the second line would
        # repeat the first to the decimal, and a duplicated number reads as a
        # broken one.
        ex_html = ""
        if abs(real - cov) >= 0.05:
            ex_html = (
                f"<span class='cov-ex' title='Coverage over the WHOLE baseline, "
                f"partial gaps included — the figure the KPI strip, the Coverage "
                f"tab, the Report and Dexter show.'>{real:.1f}%</span>"
            )
        cov_cell = (
            f'<td class="l"><div class="cov-wrap">'
            f'<div class="cov-track"><div class="cov-fill" '
            f'style="width:{min(cov, 100):.0f}%;background:{color}"></div></div>'
            f'<span class="cov-val" style="color:{color}">{cov:.1f}%</span>'
            f'</div>{ex_html}</td>'
        )
        sel_cls  = " class='sel'" if selected_bu and str(r["BU"]) == selected_bu else ""
        scope_td = (f'<td class="l"><span class="scope-pill">'
                    f'{html.escape(str(r["Scope"]))}</span></td>') if show_scope else ''
        body_rows.append(
            f'<tr{sel_cls}>'
            f'<td class="l bu">{html.escape(str(r["BU"]))}</td>'
            f'{scope_td}{nums}{cov_cell}</tr>'
        )
    return (f'<div class="bl-summary"><table>{head}'
            f'<tbody>{"".join(body_rows)}</tbody></table></div>')


@st.fragment
def render() -> None:
    # Scope drives which baseline we show: Mobile App uses a priority-based
    # baseline (separate pipeline), everything else the big_regr label baseline.
    scope, bu = global_filter.current()

    # One run at a time.  Everything below — table, tiles, coverage, frameworks,
    # pivot — reports on the run picked here, so no two populations share a page.
    # The picker takes the width it needs; the rest of the row carries what the
    # chosen run actually is, rather than being left over.
    c_pick, c_what = st.columns([5, 6], vertical_alignment="center")
    with c_pick:
        run = st.segmented_control(
            "Run", RUNS, default=RUN_BIG, required=True,
            key=f"bl_run_{scope}", label_visibility="collapsed",
        ) or RUN_BIG
    with c_what:
        # Inline <span>, not a block: a block element collapses against
        # Streamlit's -1rem markdown margin and lands below the control's centre.
        st.markdown(
            f"<span style='font-size:12.5px;color:{COLORS['muted']};"
            f"display:block;text-align:right'>{_RUN_MEANING.get(run, '')}</span>",
            unsafe_allow_html=True,
        )

    spinner = ("📱 Computing Mobile App backlog — first load can take ~30-60s, "
               "then it's cached…" if scope == "mobile_app"
               else f"Computing {run}…")
    with st.spinner(spinner):
        summary, expanded_by_bu, auto_by_bu = _run_data(run, scope)

    if summary is None or summary.empty:
        st.warning({
            RUN_SMALL: "No Small No-Regression rows yet — tick the `small_nr` "
                       "checkbox in TestRail.",
            RUN_PS:    "No Production Sanity rows yet — add the `prod_sanity` "
                       "label in TestRail.",
        }.get(run, "No baseline data found. Ensure cases have the "
                   "big_regr_desktop / big_regr_mobile labels in TestRail.")
             + "  New values appear at the next data refresh (↻ next to the tabs).")
        return

    # ── Summary table ─────────────────────────────────────────────────────────
    # Scope-filter: Microservices is computed alongside Website (so the KPI-strip
    # totals stay correct) but only SHOWN under its own scope — no more mixing it
    # into the Website table.
    scope_display = _SCOPE_DISPLAY.get(scope, "Website")
    display = summary[summary["Scope"] == scope_display].copy()
    if display.empty:
        display = summary.copy()   # safety net: never show a blank table
    # "Unknown" (the case is automated nowhere and no status explains the row)
    # is shown only when it occurs, so Total always equals the sum of the
    # columns.
    for empty_col in ("Unknown", "Playwright"):
        if empty_col in display.columns and int(display[empty_col].sum()) == 0:
            display = display.drop(columns=[empty_col])
    # Column order is scope-aware: Java/TestIM for website & microservices,
    # iOS/Android for Mobile App (see `_build_summary`).
    num_cols = [col for col in ["Total", "Automated", "Java", "TestIM",
                                "Playwright", "iOS", "Android", "Backlog",
                                "Partially Automated", "To be Updated",
                                "Not Applicable", "Unknown"]
                if col in display.columns]

    # Header + RAG legend + export on ONE row.  The CSV is a text-sized label
    # next to the legend, not a button: managers forward these numbers into decks
    # and mails, so the export must exist — but it is a secondary action and a
    # full-width button dominated the row.  (The presentation table is custom
    # HTML, which has no built-in download, hence the explicit control.)
    c_title, c_meta = st.columns([3, 4], vertical_alignment="center")
    with c_title:
        # inline-block <span>, not a block <div> — same reason as the legend
        # below: a block element collapses against Streamlit's -1rem markdown
        # margin and lands 8px below the legend it should be level with.
        section_title("All Business Units", top=0)
    # Native horizontal flex row: legend and CSV share one optically centred
    # line, right-aligned against the table's right edge.
    with c_meta, st.container(
        key="summary_export", horizontal=True, vertical_alignment="center",
        horizontal_alignment="right", gap="small",
    ):
        # <span> (inline) rather than <div>: see the note in app.py's utility
        # bar — a block element collapses against Streamlit's -1rem markdown
        # margin and sits 8px below the row's centre.
        st.markdown(
            f'<span style="font-size:12px;color:{COLORS["muted"]};'
            f'white-space:nowrap">'
            f'🟢 ≥ {COVERAGE_TARGET:.0f}% &nbsp;·&nbsp; 🟡 ≥ 60% &nbsp;·&nbsp; '
            f'🔴 below</span>',
            unsafe_allow_html=True,
        )
        st.download_button(
            "⬇ CSV",
            display.to_csv(index=False).encode("utf-8"),
            file_name=f"automation_baseline_{scope}.csv",
            mime="text/csv",
            help="Download the table above, exactly as shown, as a CSV.",
        )
    st.markdown(_summary_table_html(display, num_cols, selected_bu=bu),
                unsafe_allow_html=True)

    st.divider()

    # ── Detail — follows the GLOBAL scope + BU selector ───────────────────────
    section_title("Detail by Business Unit")
    exp = expanded_by_bu.get((bu, scope))
    if exp is None or exp.empty:
        st.info(f"No {run} rows for **{bu}** in this scope.")
        return
    _detail_view(bu, scope, expanded_by_bu, auto_by_bu, run=run)

    # (The TestRail hygiene checklist now lives in the utility bar next to
    # "Updated …" — see `_freshness_label` in app.py.)
