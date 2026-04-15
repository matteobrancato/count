"""Evaluate Rules against TestRail cases and produce normalised expansion rows.

A single case can expand into multiple rows (one per matching country × device).
This is the only layer that knows about the `Both` device rule and country
expansion logic (see module docstring comments for edge cases).

Output DataFrame columns (stable contract used by metrics & UI):

    case_id, title, url, section_id, section_path, type_id, priority_id,
    priority_label, deprecated, suite_id, bu, scope, framework, rule_name,
    country_token, country_label, device, automation_tool,
    is_automated, is_regression, is_prod_sanity, status_value
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd
import streamlit as st

from . import testrail_client as tr
from .bu_rules import Rule, ALL_RULES
from .field_resolver import FieldRegistry, get_registry


# --------------------------------------------------------------------- helpers
DEVICE_FIELD_CANDIDATES = ("Device type", "Device Type", "Device")
AUTOMATION_TOOL_CANDIDATES = ("Automation Tool", "Automation tool")
PROD_SANITY_CANDIDATES = ("Prod Sanity", "Production Sanity")
MULTI_COUNTRIES_CANDIDATES = ("multi_countries", "Multi Countries", "Multi_countries")
DEPRECATED_CANDIDATES = ("Deprecated",)


def _get_multi_countries(case: dict, reg: FieldRegistry) -> list[str]:
    meta = reg.field("multi_countries")
    for cand in MULTI_COUNTRIES_CANDIDATES:
        meta = meta or reg.field(cand)
        if meta:
            break
    if not meta:
        return []
    raw = case.get(meta.system_name)
    if raw is None:
        return []
    # multi-select: list of ids OR newline/comma-separated ids
    if isinstance(raw, list):
        ids = [int(x) for x in raw]
    elif isinstance(raw, str):
        ids = []
        for token in raw.replace("\n", ",").split(","):
            token = token.strip()
            if token.isdigit():
                ids.append(int(token))
    else:
        ids = []
    return [meta.values_by_id.get(i, "") for i in ids if i in meta.values_by_id]


def _get_single_label(case: dict, reg: FieldRegistry, candidates: tuple[str, ...]) -> str | None:
    for cand in candidates:
        meta = reg.field(cand)
        if not meta:
            continue
        raw = case.get(meta.system_name)
        if raw is None:
            return None
        if isinstance(raw, int):
            return meta.values_by_id.get(raw)
        if isinstance(raw, str) and raw.strip().isdigit():
            return meta.values_by_id.get(int(raw.strip()))
        if isinstance(raw, str):
            return raw
    return None


def _is_deprecated(case: dict, reg: FieldRegistry) -> bool:
    meta = reg.field("Deprecated")
    if not meta:
        return False
    raw = case.get(meta.system_name)
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        label = meta.values_by_id.get(raw, "").strip().lower()
        return label in ("yes", "true", "deprecated")
    if isinstance(raw, str):
        return raw.strip().lower() in ("yes", "true", "deprecated")
    return False


def _devices_for(case: dict, reg: FieldRegistry) -> list[str]:
    """Return ["Desktop"], ["Mobile"], or ["Desktop", "Mobile"] (Both expands).

    Cases with no device info fall back to ["Unspecified"] so they still surface
    somewhere rather than silently vanishing from the totals.
    """
    label = _get_single_label(case, reg, DEVICE_FIELD_CANDIDATES)
    if not label:
        return ["Unspecified"]
    low = label.strip().lower()
    if low == "both":
        return ["Desktop", "Mobile"]
    if low.startswith("desktop"):
        return ["Desktop"]
    if low.startswith("mobile"):
        return ["Mobile"]
    return [label]


def _case_url(base_url: str, case_id: int) -> str:
    return f"{base_url.rstrip('/')}/index.php?/cases/view/{case_id}"


# --------------------------------------------------------------------- matching
def _rule_matches(case: dict, rule: Rule, reg: FieldRegistry) -> tuple[bool, list[str]]:
    """Return (match, matched_country_tokens). Empty token list = no country filter."""
    # Type
    if rule.type_filter:
        expected = {reg.type_id(t) for t in rule.type_filter}
        expected.discard(None)
        if case.get("type_id") not in expected:
            return False, []
    # Deprecated
    if _is_deprecated(case, reg) != (rule.deprecated.strip().lower() == "yes"):
        return False, []
    # Priority (optional filter)
    if rule.priority_filter:
        expected = {reg.priority_id(p) for p in rule.priority_filter}
        expected.discard(None)
        if case.get("priority_id") not in expected:
            return False, []
    # Automation status
    status_meta = reg.field(rule.status_field_label)
    if not status_meta:
        # Field missing altogether — rule cannot apply
        return False, []
    raw = case.get(status_meta.system_name)
    if raw is None:
        return False, []
    allowed_ids = reg.status_value_ids(rule.status_field_label, rule.automated_values)
    if isinstance(raw, list):
        if not any(int(v) in allowed_ids for v in raw):
            return False, []
    elif isinstance(raw, int):
        if raw not in allowed_ids:
            return False, []
    elif isinstance(raw, str) and raw.strip().isdigit():
        if int(raw.strip()) not in allowed_ids:
            return False, []
    else:
        return False, []

    # Country filter
    if rule.countries_filter:
        tokens = set(_get_multi_countries(case, reg))
        matched = [c for c in rule.countries_filter if c in tokens]
        if not matched:
            return False, []
        return True, matched

    return True, []


# --------------------------------------------------------------------- expansion
def _expand_rows(case: dict, rule: Rule, reg: FieldRegistry,
                 matched_countries: list[str], base_url: str) -> list[dict]:
    devices = _devices_for(case, reg)
    prod_sanity = _get_single_label(case, reg, PROD_SANITY_CANDIDATES)
    prod_sanity_yes = bool(prod_sanity and prod_sanity.strip().lower() == "yes")
    automation_tool = _get_single_label(case, reg, AUTOMATION_TOOL_CANDIDATES)
    priority_label = reg.priority_id_to_label.get(int(case.get("priority_id") or 0))
    status_meta = reg.field(rule.status_field_label)
    raw_status = case.get(status_meta.system_name) if status_meta else None
    if isinstance(raw_status, list) and raw_status:
        status_label = status_meta.values_by_id.get(int(raw_status[0]))
    elif isinstance(raw_status, int):
        status_label = status_meta.values_by_id.get(raw_status)
    else:
        status_label = None

    # Country expansion: tokens that passed the filter, or a single implicit country
    if matched_countries:
        country_pairs = [(tok, rule.country_labels.get(tok, tok)) for tok in matched_countries]
    elif rule.implicit_country:
        country_pairs = [(rule.implicit_country, rule.implicit_country)]
    else:
        # Next Gen / TP-Java / mobile-app: no country expansion; tag with scope token
        country_pairs = [("__ALL__", rule.bu)]

    rows: list[dict] = []
    for tok, label in country_pairs:
        for dev in devices:
            rows.append({
                "case_id": int(case["id"]),
                "title": case.get("title"),
                "url": _case_url(base_url, int(case["id"])),
                "section_id": case.get("section_id"),
                "type_id": case.get("type_id"),
                "priority_id": case.get("priority_id"),
                "priority_label": priority_label,
                "deprecated": _is_deprecated(case, reg),
                "suite_id": rule.suite_id,
                "bu": rule.bu,
                "scope": rule.scope,
                "framework": rule.framework,
                "rule_name": rule.name,
                "country_token": tok,
                "country_label": label,
                "device": dev,
                "automation_tool": automation_tool,
                "is_automated": True,
                "is_regression": True,
                "is_prod_sanity": prod_sanity_yes,
                "status_value": status_label,
            })
    return rows


# --------------------------------------------------------------------- public API
@dataclass
class ExpansionResult:
    automated: pd.DataFrame   # one row per (case × matched country × device), only automated
    raw_cases: pd.DataFrame   # one row per RAW case (all of them), for Tab 1 pivot + coverage


def _raw_case_row(case: dict, reg: FieldRegistry, suite_id: int, base_url: str) -> dict:
    devices = _devices_for(case, reg)
    device_label = "Both" if len(devices) == 2 else devices[0]
    priority_label = reg.priority_id_to_label.get(int(case.get("priority_id") or 0))
    type_label = None
    for t_name, t_id in reg.type_label_to_id.items():
        if t_id == case.get("type_id"):
            type_label = t_name.title()
            break
    return {
        "case_id": int(case["id"]),
        "title": case.get("title"),
        "url": _case_url(base_url, int(case["id"])),
        "suite_id": suite_id,
        "section_id": case.get("section_id"),
        "type_id": case.get("type_id"),
        "type_label": type_label,
        "priority_id": case.get("priority_id"),
        "priority_label": priority_label,
        "deprecated": _is_deprecated(case, reg),
        "device": device_label,
        "device_expanded": devices,
        "multi_countries": _get_multi_countries(case, reg),
        "automation_tool": _get_single_label(case, reg, AUTOMATION_TOOL_CANDIDATES),
        "prod_sanity": _get_single_label(case, reg, PROD_SANITY_CANDIDATES),
    }


def _section_path_lookup(sections: list[dict]) -> dict[int, str]:
    by_id = {int(s["id"]): s for s in sections}
    cache: dict[int, str] = {}

    def path(sid: int) -> str:
        if sid in cache:
            return cache[sid]
        node = by_id.get(sid)
        if not node:
            return ""
        parent = node.get("parent_id")
        prefix = path(int(parent)) + " > " if parent else ""
        cache[sid] = (prefix + (node.get("name") or "")).strip(" >")
        return cache[sid]

    for sid in list(by_id.keys()):
        path(sid)
    return cache


@st.cache_data(show_spinner="Fetching and expanding cases…", ttl=600)
def evaluate_rules(rule_names: tuple[str, ...]) -> ExpansionResult:
    """Fetch every suite referenced by the given rules and evaluate them.

    Returns both:
        - `automated`: post-filter rows expanded per country/device (used for metrics)
        - `raw_cases`: every raw case fetched (one row each) — used by Tab 1's pivot view
    """
    reg = get_registry()
    rules = [r for r in ALL_RULES if r.name in rule_names]
    # base URL for building case links
    base_url = tr.TestRailCredentials.from_secrets().base_url

    # Group rules by suite so we download each suite only once
    suites = sorted({r.suite_id for r in rules})
    suite_to_project = {sid: tr.resolve_project_id(sid) for sid in suites}
    suite_cases: dict[int, list[dict]] = {}
    suite_sections: dict[int, dict[int, str]] = {}
    for sid in suites:
        pid = suite_to_project[sid]
        suite_cases[sid] = tr.fetch_cases(pid, sid)
        suite_sections[sid] = _section_path_lookup(tr.fetch_sections(pid, sid))

    automated_rows: list[dict] = []
    raw_rows: list[dict] = []
    seen_raw: set[tuple[int, int]] = set()  # (suite_id, case_id)

    for rule in rules:
        cases = suite_cases.get(rule.suite_id, [])
        sect_map = suite_sections.get(rule.suite_id, {})
        for case in cases:
            cid = int(case["id"])
            key = (rule.suite_id, cid)
            if key not in seen_raw:
                raw = _raw_case_row(case, reg, rule.suite_id, base_url)
                raw["section_path"] = sect_map.get(int(case.get("section_id") or 0), "")
                raw_rows.append(raw)
                seen_raw.add(key)
            matched, countries = _rule_matches(case, rule, reg)
            if not matched:
                continue
            for row in _expand_rows(case, rule, reg, countries, base_url):
                row["section_path"] = sect_map.get(int(case.get("section_id") or 0), "")
                automated_rows.append(row)

    automated_df = pd.DataFrame(automated_rows)
    raw_df = pd.DataFrame(raw_rows)
    return ExpansionResult(automated=automated_df, raw_cases=raw_df)
