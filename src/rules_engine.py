"""Evaluate Rules against TestRail cases and produce normalised expansion rows.

Field notes confirmed from TestRail Customizations screenshots (April 2026):
  - Deprecated  : Checkbox → bool  (custom_deprecated)
  - Prod Sanity : Checkbox → bool  (custom_prod_sanity)
  - Device      : Dropdown, label "Device" → custom_device
                  Values include "Both" (expands to Desktop + Mobile),
                  "Desktop", "Mobile", "Desktop only", "Mobile only"
  - multi_countries : Multi-select → list of int IDs (custom_multi_countries)
  - Automation MAPP Tool : Dropdown (custom_automation_mapp_tool)

Output DataFrame columns:
    case_id, title, url, section_id, section_path,
    type_id, priority_id, priority_label,
    deprecated, suite_id, bu, scope, framework, rule_name,
    country_token, country_label, device,
    automation_tool, is_automated, is_regression, is_prod_sanity, status_value
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import pandas as pd
import streamlit as st

from . import testrail_client as tr
from .bu_rules import Rule, ALL_RULES
from .field_resolver import FieldRegistry, get_registry


# ----------------------------------------------------------------- field labels
# Use the EXACT label from the TestRail Customizations page.
_DEVICE_LABEL        = "Device"
_DEPRECATED_LABEL    = "Deprecated"
_PROD_SANITY_LABEL   = "Test Automation PRD Run"
_MULTI_COUNTRIES_LABEL = "multi_countries"
_AUTOMATION_TOOL_LABEL = "Automation MAPP Tool"  # screenshot: "Automation MAPP Tool"


# ----------------------------------------------------------------- helpers
def _get_country_tokens(
    case: dict,
    reg: FieldRegistry,
    field_label: str = _MULTI_COUNTRIES_LABEL,
    project_id: int | None = None,
) -> list[str]:
    """Return list of country token strings from a multi-select country field.

    Supports any multi-select field by label:
      - "multi_countries"           → the standard field (default)
      - "Country Validation"        → custom_country_validation  (MRN Java)
      - "Testim Country Coverage"   → custom_case_country_coverage_testim  (MRN TestIM)

    *project_id* selects the correct per-project value map so that the same
    integer ID resolves to the right label in each suite.
    """
    # Try by label first; fall back to known system names for the two MRN fields
    meta = reg.field(field_label)
    if not meta and field_label == "Country Validation":
        meta = reg.field("custom_country_validation")
    if not meta and field_label == "Testim Country Coverage":
        meta = reg.field("custom_case_country_coverage_testim")
    if not meta:
        return []
    raw = case.get(meta.system_name)
    if raw is None:
        return []
    # TestRail multi-select returns a list of integer IDs (or None if empty)
    if isinstance(raw, list):
        ids = []
        for x in raw:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                pass
    elif isinstance(raw, str):
        ids = []
        for token in raw.replace("\n", ",").split(","):
            token = token.strip()
            if token.isdigit():
                ids.append(int(token))
    else:
        return []
    val_map = meta.values_for_project(project_id)
    return [val_map[i] for i in ids if i in val_map]


# Keep backward-compatible alias used elsewhere
def _get_multi_countries(case: dict, reg: FieldRegistry, project_id: int | None = None) -> list[str]:
    return _get_country_tokens(case, reg, _MULTI_COUNTRIES_LABEL, project_id)


def _is_deprecated(case: dict, reg: FieldRegistry) -> bool:
    """Deprecated is a Checkbox field → bool value in the API."""
    meta = reg.field(_DEPRECATED_LABEL) or reg.field("custom_deprecated")
    if not meta:
        return False
    raw = case.get(meta.system_name)
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    # Fallback: some older TestRail versions return 0/1
    if isinstance(raw, int):
        return raw == 1
    if isinstance(raw, str):
        return raw.strip().lower() in ("yes", "true", "1")
    return False


def _get_prod_sanity(case: dict, reg: FieldRegistry) -> bool:
    """Test Automation PRD Run is a Checkbox field → bool value in the API.

    Try the human label first; fall back to known system name candidates so a
    label mismatch in TestRail doesn't silently zero-out all prod-sanity counts.
    """
    meta = (
        reg.field(_PROD_SANITY_LABEL)
        or reg.field("custom_test_automation_prd_run")   # confirmed system name
    )
    if not meta:
        return False
    raw = case.get(meta.system_name)
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return raw == 1
    if isinstance(raw, str):
        return raw.strip().lower() in ("yes", "true", "1")
    return False


def _get_automation_tool(case: dict, reg: FieldRegistry) -> str | None:
    """Automation MAPP Tool dropdown → string label."""
    meta = reg.field(_AUTOMATION_TOOL_LABEL) or reg.field("custom_case_automation_mapp_tool")
    if not meta:
        return None
    raw = case.get(meta.system_name)
    if isinstance(raw, int):
        return meta.values_by_id.get(raw)
    return None


def _devices_for(case: dict, reg: FieldRegistry) -> list[str]:
    """Expand the Device dropdown:
    - "Both"           → ["Desktop", "Mobile"]
    - "Desktop" / "Desktop only" → ["Desktop"]
    - "Mobile" / "Mobile only"   → ["Mobile"]
    - anything else / missing    → ["Unspecified"]
    """
    meta = reg.field(_DEVICE_LABEL)
    if not meta:
        return ["Unspecified"]
    raw = case.get(meta.system_name)
    if raw is None:
        return ["Unspecified"]
    if isinstance(raw, int):
        label = meta.values_by_id.get(raw, "")
    elif isinstance(raw, str):
        label = raw
    else:
        return ["Unspecified"]

    low = label.strip().lower()
    if "both" in low:
        return ["Desktop", "Mobile"]
    if low.startswith("desktop"):
        return ["Desktop"]
    if low.startswith("mobile"):
        return ["Mobile"]
    return ["Unspecified"]


def _case_url(base_url: str, case_id: int) -> str:
    return f"{base_url.rstrip('/')}/index.php?/cases/view/{case_id}"


# ----------------------------------------------------------------- matching
def _rule_matches(
    case: dict, rule: Rule, reg: FieldRegistry, project_id: int | None = None
) -> tuple[bool, list[str]]:
    """Return (match, matched_country_tokens).

    Steps (short-circuit on first failure):
      1. type_filter
      2. NOT deprecated
      3. automation status value in allowed set
      4. multi_countries intersects countries_filter (if set)
    """
    # 1. Type — skip gracefully if type names can't be resolved (e.g. custom type fields
    #    per BU like "Type WTR / Type MRN" used instead of the standard type_id).
    if rule.type_filter:
        expected = {reg.type_id(t) for t in rule.type_filter} - {None}
        if expected and case.get("type_id") not in expected:
            return False, []
        # If expected is empty, resolution failed → don't reject the case; rely on
        # the automation-status filter (which is already BU/framework-specific).

    # 2. Deprecated (must be False)
    if _is_deprecated(case, reg):
        return False, []

    # 3. Automation status
    status_meta = reg.field(rule.status_field_label)
    if not status_meta:
        return False, []
    raw = case.get(status_meta.system_name)
    if raw is None:
        return False, []
    allowed_ids = reg.status_value_ids(rule.status_field_label, rule.automated_values)
    if not allowed_ids:
        # Field found but no value IDs resolved — label mismatch in automated_values
        return False, []
    if isinstance(raw, list):
        if not any(int(v) in allowed_ids for v in raw if str(v).isdigit()):
            return False, []
    elif isinstance(raw, int):
        if raw not in allowed_ids:
            return False, []
    elif isinstance(raw, str) and raw.strip().isdigit():
        if int(raw.strip()) not in allowed_ids:
            return False, []
    else:
        return False, []

    # 4. Country filter — read from the field specified by rule.country_field_label
    if rule.countries_filter:
        tokens = set(_get_country_tokens(case, reg, rule.country_field_label, project_id))
        matched = [c for c in rule.countries_filter if c in tokens]
        if not matched:
            return False, []
        return True, matched

    return True, []


# ----------------------------------------------------------------- expansion
def _expand_rows(
    case: dict, rule: Rule, reg: FieldRegistry,
    matched_countries: list[str], base_url: str,
    project_id: int | None = None,
) -> list[dict]:
    # TestIM Desktop/Mobile framework → device is determined by the framework, not the
    # Device field.  A TestIM Desktop test always runs on Desktop; Mobile always Mobile.
    # Expanding "Both" for TestIM would double-count cases that happen to have Device=Both.
    if rule.framework == "testim_desktop":
        devices         = ["Desktop"]
        device_original = "Desktop"
    elif rule.framework == "testim_mobile":
        devices         = ["Mobile"]
        device_original = "Mobile"
    else:
        devices         = _devices_for(case, reg)
        # Track the original Device field value before expansion
        device_original = "Both" if len(devices) == 2 else devices[0]
    prod_sanity_yes  = _get_prod_sanity(case, reg)
    automation_tool  = _get_automation_tool(case, reg)
    priority_label   = reg.priority_id_to_label.get(int(case.get("priority_id") or 0))

    status_meta  = reg.field(rule.status_field_label)
    raw_status   = case.get(status_meta.system_name) if status_meta else None
    status_label = None
    if status_meta and isinstance(raw_status, int):
        status_label = status_meta.values_by_id.get(raw_status)

    # Country pairs: (token, display_label)
    if matched_countries:
        country_pairs = [(tok, rule.country_labels.get(tok, tok)) for tok in matched_countries]
    elif rule.implicit_country:
        country_pairs = [(rule.implicit_country, rule.implicit_country)]
    else:
        country_pairs = [("__ALL__", rule.bu)]

    rows: list[dict] = []
    for tok, label in country_pairs:
        for dev in devices:
            rows.append({
                "case_id":        int(case["id"]),
                "title":          case.get("title"),
                "url":            _case_url(base_url, int(case["id"])),
                "section_id":     case.get("section_id"),
                "type_id":        case.get("type_id"),
                "priority_id":    case.get("priority_id"),
                "priority_label": priority_label,
                "deprecated":     False,  # already filtered above
                "suite_id":       rule.suite_id,
                "bu":             rule.bu,
                "scope":          rule.scope,
                "framework":      rule.framework,
                "rule_name":      rule.name,
                "country_token":  tok,
                "country_label":  label,
                "device":         dev,
                "device_original": device_original,
                "automation_tool": automation_tool,
                "is_automated":   True,
                "is_regression":  True,
                "is_prod_sanity": prod_sanity_yes,
                "status_value":   status_label,
            })
    return rows


# ----------------------------------------------------------------- raw case row
def _raw_case_row(case: dict, reg: FieldRegistry, suite_id: int, base_url: str,
                  project_id: int | None = None) -> dict:
    devices     = _devices_for(case, reg)
    dev_label   = "Both" if len(devices) == 2 else devices[0]
    priority_label = reg.priority_id_to_label.get(int(case.get("priority_id") or 0))
    # Resolve type_id → human label
    type_label = None
    for t_name, t_id in reg.type_label_to_id.items():
        if t_id == case.get("type_id"):
            type_label = t_name.title()
            break
    # Collect all automation status values for display in Tab 1
    auto_status_fields = {
        lbl: reg.field(lbl)
        for lbl in [
            "Automation Status", "Automation Status KV SPR", "Automation Status TP",
            "Automation Status ICI", "Automation Status MFR", "Automation Status MRN SPR",
            "Automation Status SD", "Automation Status TPS", "Automation Status DRG",
            "Automation Status Testim Desktop", "Automation Status Testim Mobile View",
        ]
        if reg.field(lbl)
    }
    auto_status_resolved: dict[str, str | None] = {}
    for lbl, meta in auto_status_fields.items():
        raw = case.get(meta.system_name)
        if isinstance(raw, int):
            auto_status_resolved[lbl] = meta.values_by_id.get(raw)
        else:
            auto_status_resolved[lbl] = None

    return {
        "case_id":       int(case["id"]),
        "title":         case.get("title"),
        "url":           _case_url(base_url, int(case["id"])),
        "suite_id":      suite_id,
        "section_id":    case.get("section_id"),
        "type_id":       case.get("type_id"),
        "type_label":    type_label,
        "priority_id":   case.get("priority_id"),
        "priority_label": priority_label,
        "deprecated":    _is_deprecated(case, reg),
        "device":        dev_label,
        "multi_countries": _get_multi_countries(case, reg, project_id),
        "automation_tool": _get_automation_tool(case, reg),
        "prod_sanity":   _get_prod_sanity(case, reg),
        **{f"status_{k}": v for k, v in auto_status_resolved.items()},
    }


# ----------------------------------------------------------------- section path
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


# ----------------------------------------------------------------- public API
@dataclass
class ExpansionResult:
    automated: pd.DataFrame   # expanded (case × country × device), only automated
    raw_cases: pd.DataFrame   # every case in the suites (for Tab 1 pivot)


def _fetch_suite_data(sid: int, pid: int) -> tuple[int, list[dict], dict[int, str]]:
    """Fetch cases + section map for one suite — runs inside a thread pool."""
    cases    = tr.fetch_cases(pid, sid)
    sections = _section_path_lookup(tr.fetch_sections(pid, sid))
    return sid, cases, sections


@st.cache_data(show_spinner="Fetching and expanding cases…", ttl=3600)
def evaluate_rules(rule_names: tuple[str, ...]) -> ExpansionResult:
    reg      = get_registry()
    rules    = [r for r in ALL_RULES if r.name in rule_names]
    base_url = tr.TestRailCredentials.from_secrets().base_url

    suites = sorted({r.suite_id for r in rules})

    # Resolve project IDs in parallel (each is a cached API call)
    with ThreadPoolExecutor(max_workers=min(len(suites), 8)) as pool:
        pid_futures = {sid: pool.submit(tr.resolve_project_id, sid) for sid in suites}
    suite_to_project = {sid: f.result() for sid, f in pid_futures.items()}

    # Fetch cases + sections for every suite in parallel
    suite_cases:    dict[int, list[dict]]     = {}
    suite_sections: dict[int, dict[int, str]] = {}
    with ThreadPoolExecutor(max_workers=min(len(suites), 8)) as pool:
        futures = {
            pool.submit(_fetch_suite_data, sid, suite_to_project[sid]): sid
            for sid in suites
        }
        for future in as_completed(futures):
            sid, cases, sections = future.result()
            suite_cases[sid]    = cases
            suite_sections[sid] = sections

    automated_rows: list[dict] = []
    raw_rows:       list[dict] = []
    seen_raw: set[tuple[int, int]] = set()

    for rule in rules:
        cases    = suite_cases.get(rule.suite_id, [])
        sect_map = suite_sections.get(rule.suite_id, {})
        pid      = suite_to_project.get(rule.suite_id)

        for case in cases:
            cid = int(case["id"])
            key = (rule.suite_id, cid)
            if key not in seen_raw:
                raw = _raw_case_row(case, reg, rule.suite_id, base_url, project_id=pid)
                raw["section_path"] = sect_map.get(int(case.get("section_id") or 0), "")
                raw_rows.append(raw)
                seen_raw.add(key)

            matched, countries = _rule_matches(case, rule, reg, project_id=pid)
            if not matched:
                continue
            for row in _expand_rows(case, rule, reg, countries, base_url, project_id=pid):
                row["section_path"] = sect_map.get(int(case.get("section_id") or 0), "")
                automated_rows.append(row)

    return ExpansionResult(
        automated = pd.DataFrame(automated_rows) if automated_rows else pd.DataFrame(),
        raw_cases = pd.DataFrame(raw_rows)        if raw_rows       else pd.DataFrame(),
    )
