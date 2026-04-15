"""Resolve TestRail custom field labels → system names and dropdown-value labels → ids.

TestRail returns custom field values as integer ids for dropdowns and as a '\n'-delimited
ids-list for multi-selects. The human labels live in the field configs. We build the
mapping once per session (cached) from /get_case_fields + /get_case_types + /get_priorities.

Lookups are case-insensitive and whitespace-normalised so small label drifts
("Automation Status Testim Mobile" vs "Automation Status Testim Mobile View") still resolve.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import streamlit as st

from . import testrail_client as tr


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


@dataclass
class FieldMeta:
    system_name: str                 # e.g. "custom_automation_status_testim_desktop"
    label: str                       # canonical human label
    type_id: int                     # TestRail field type (6=dropdown, 12=multi-select, ...)
    values_by_id: dict[int, str]     # {1: "Automated", 2: "Automated DEV", ...}
    ids_by_label: dict[str, int]     # normalised label → id


@dataclass
class FieldRegistry:
    fields_by_label: dict[str, FieldMeta] = field(default_factory=dict)
    fields_by_system: dict[str, FieldMeta] = field(default_factory=dict)
    type_label_to_id: dict[str, int] = field(default_factory=dict)
    priority_label_to_id: dict[str, int] = field(default_factory=dict)
    priority_id_to_label: dict[int, str] = field(default_factory=dict)

    # ---------------------------------------------------------------- lookups
    def field(self, label_or_system: str) -> FieldMeta | None:
        key = _norm(label_or_system)
        if key in self.fields_by_label:
            return self.fields_by_label[key]
        # allow passing system names directly
        return self.fields_by_system.get(label_or_system) or self.fields_by_system.get(
            f"custom_{label_or_system}"
        )

    def require_field(self, label_or_system: str) -> FieldMeta:
        meta = self.field(label_or_system)
        if not meta:
            raise KeyError(
                f"TestRail custom field not found: {label_or_system!r}. "
                f"Known labels: {sorted(self.fields_by_label.keys())[:20]}…"
            )
        return meta

    def status_value_ids(self, field_label: str, value_labels: list[str]) -> set[int]:
        meta = self.require_field(field_label)
        out: set[int] = set()
        for v in value_labels:
            vid = meta.ids_by_label.get(_norm(v))
            if vid is not None:
                out.add(vid)
        return out

    def type_id(self, label: str) -> int | None:
        return self.type_label_to_id.get(_norm(label))

    def priority_id(self, label: str) -> int | None:
        return self.priority_label_to_id.get(_norm(label))


# --------------------------------------------------------------------- parsing
def _parse_dropdown_configs(raw_field: dict) -> tuple[dict[int, str], dict[str, int]]:
    """TestRail dropdown configs: items is a newline-separated list of 'id, label' lines."""
    values_by_id: dict[int, str] = {}
    ids_by_label: dict[str, int] = {}
    configs = raw_field.get("configs") or []
    for cfg in configs:
        opts = (cfg or {}).get("options") or {}
        items = opts.get("items") or ""
        for line in items.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\d+)\s*,\s*(.+)$", line)
            if not m:
                continue
            vid = int(m.group(1))
            label = m.group(2).strip()
            values_by_id[vid] = label
            ids_by_label[_norm(label)] = vid
    return values_by_id, ids_by_label


@st.cache_data(show_spinner="Resolving TestRail custom fields…", ttl=900)
def _build_registry_raw() -> dict:
    fields = tr.fetch_case_fields()
    types = tr.fetch_case_types()
    prios = tr.fetch_priorities()

    by_label: dict[str, FieldMeta] = {}
    by_system: dict[str, FieldMeta] = {}
    for f in fields:
        system_name = f.get("system_name") or f.get("name")
        if not system_name:
            continue
        label = f.get("label") or f.get("name") or system_name
        values_by_id, ids_by_label = _parse_dropdown_configs(f)
        meta = FieldMeta(
            system_name=system_name,
            label=label,
            type_id=int(f.get("type_id") or 0),
            values_by_id=values_by_id,
            ids_by_label=ids_by_label,
        )
        by_label[_norm(label)] = meta
        # also index under the plain name (what TestRail docs call "name")
        by_label[_norm(f.get("name") or "")] = meta
        by_system[system_name] = meta

    type_label_to_id = {_norm(t["name"]): int(t["id"]) for t in types}
    priority_label_to_id = {_norm(p["name"]): int(p["id"]) for p in prios}
    # fall back on short names too (Highest, High…)
    for p in prios:
        short = p.get("short_name") or p.get("name")
        priority_label_to_id.setdefault(_norm(short), int(p["id"]))
    priority_id_to_label = {int(p["id"]): p.get("name", str(p["id"])) for p in prios}

    return {
        "fields_by_label": by_label,
        "fields_by_system": by_system,
        "type_label_to_id": type_label_to_id,
        "priority_label_to_id": priority_label_to_id,
        "priority_id_to_label": priority_id_to_label,
    }


def get_registry() -> FieldRegistry:
    raw = _build_registry_raw()
    return FieldRegistry(**raw)
