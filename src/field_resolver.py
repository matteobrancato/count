"""Resolve TestRail custom field labels → system names and dropdown-value labels → ids.

TestRail returns custom field values as integer ids for dropdowns and as a '\n'-delimited
ids-list for multi-selects. The human labels live in the field configs. We build the
mapping once per session (cached) from /get_case_fields + /get_case_types + /get_priorities.

Lookups are case-insensitive and whitespace-normalised so small label drifts
("Automation Status Testim Mobile" vs "Automation Status Testim Mobile View") still resolve.

IMPORTANT – project-aware configs
----------------------------------
The `multi_countries` (and a few other) fields have *per-project* configs: the same integer
ID means different country tokens in different projects.  For example, ID=3 means "TP"
(Trekpleister) in the KV/TKP project but "MRN" in other projects.

`FieldMeta` therefore stores every raw config alongside its context so callers can ask
`field.values_for_project(project_id)` and get the correct label mapping for that suite.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import streamlit as st

from . import testrail_client as tr


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# ---------------------------------------------------------------------- config meta
@dataclass
class ConfigMeta:
    """One element of the `configs` array returned by /get_case_fields."""
    is_global: bool
    project_ids: list         # list[int] — empty when is_global=True
    values_by_id: dict        # {int: str}
    ids_by_label: dict        # {str: int} — normalised label


# ---------------------------------------------------------------------- field meta
@dataclass
class FieldMeta:
    system_name: str                 # e.g. "custom_automation_status_testim"
    label: str                       # canonical human label
    type_id: int                     # TestRail field type (6=dropdown, 12=multi-select, …)
    values_by_id: dict               # merged fallback {int: str}
    ids_by_label: dict               # merged fallback {str (normalised): int}
    configs: list = field(default_factory=list)  # list[ConfigMeta]

    # ---------------------------------------------------------------- project-aware lookup
    def values_for_project(self, project_id: int | None) -> dict:
        """Return the ID→label mapping that applies to *project_id*.

        Resolution order:
          1. Project-specific config whose project_ids includes project_id.
          2. The largest global config (most entries → most complete).
          3. Merged fallback (union of all configs, last-wins per ID).
        """
        if self.configs:
            if project_id is not None:
                for cfg in self.configs:
                    if not cfg.is_global and project_id in cfg.project_ids:
                        return cfg.values_by_id
            # Largest global config
            global_cfgs = [c for c in self.configs if c.is_global]
            if global_cfgs:
                return max(global_cfgs, key=lambda c: len(c.values_by_id)).values_by_id
        return self.values_by_id

    def ids_for_project(self, project_id: int | None) -> dict:
        """Return the label→ID mapping that applies to *project_id*."""
        if self.configs:
            if project_id is not None:
                for cfg in self.configs:
                    if not cfg.is_global and project_id in cfg.project_ids:
                        return cfg.ids_by_label
            global_cfgs = [c for c in self.configs if c.is_global]
            if global_cfgs:
                return max(global_cfgs, key=lambda c: len(c.values_by_id)).ids_by_label
        return self.ids_by_label


# ---------------------------------------------------------------------- registry
@dataclass
class FieldRegistry:
    fields_by_label: dict = field(default_factory=dict)   # {str: FieldMeta}
    fields_by_system: dict = field(default_factory=dict)  # {str: FieldMeta}
    type_label_to_id: dict = field(default_factory=dict)
    priority_label_to_id: dict = field(default_factory=dict)
    priority_id_to_label: dict = field(default_factory=dict)

    # ---------------------------------------------------------------- lookups
    def field(self, label_or_system: str) -> FieldMeta | None:
        key = _norm(label_or_system)
        if key in self.fields_by_label:
            return self.fields_by_label[key]
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

    def status_value_ids(self, field_label: str, value_labels: list[str]) -> set:
        """Resolve human value labels → numeric IDs using the merged (all-projects) map."""
        meta = self.require_field(field_label)
        out: set = set()
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
def _parse_all_configs(raw_field: dict) -> tuple[dict, dict, list]:
    """Parse all per-project and global configs from a raw TestRail field dict.

    Returns:
        merged_by_id   – {int: str} union of every config (last-write-wins per ID)
        merged_by_label – {str: int} same, normalised labels
        config_metas   – list[ConfigMeta] one entry per config block, preserving context
    """
    merged_by_id: dict = {}
    merged_by_label: dict = {}
    config_metas: list = []

    configs = raw_field.get("configs") or []
    for cfg in configs:
        ctx = (cfg or {}).get("context") or {}
        is_global = bool(ctx.get("is_global", True))
        try:
            project_ids = [int(p) for p in (ctx.get("project_ids") or [])]
        except (TypeError, ValueError):
            project_ids = []

        opts = (cfg or {}).get("options") or {}
        items = opts.get("items") or ""

        cfg_by_id: dict = {}
        cfg_by_label: dict = {}
        for line in items.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\d+)\s*,\s*(.+)$", line)
            if not m:
                continue
            vid = int(m.group(1))
            lbl = m.group(2).strip()
            cfg_by_id[vid] = lbl
            cfg_by_label[_norm(lbl)] = vid
            # Merge into the global fallback dicts
            merged_by_id[vid] = lbl
            merged_by_label[_norm(lbl)] = vid

        if cfg_by_id:
            config_metas.append(ConfigMeta(
                is_global=is_global,
                project_ids=project_ids,
                values_by_id=cfg_by_id,
                ids_by_label=cfg_by_label,
            ))

    return merged_by_id, merged_by_label, config_metas


# Keep the old name as an alias so any stray import doesn't break.
def _parse_dropdown_configs(raw_field: dict) -> tuple[dict, dict]:
    v, i, _ = _parse_all_configs(raw_field)
    return v, i


@st.cache_data(show_spinner="Resolving TestRail custom fields…", ttl=900)
def _build_registry_raw() -> dict:
    fields = tr.fetch_case_fields()
    types  = tr.fetch_case_types()
    prios  = tr.fetch_priorities()

    by_label: dict = {}
    by_system: dict = {}
    for f in fields:
        system_name = f.get("system_name") or f.get("name")
        if not system_name:
            continue
        label = f.get("label") or f.get("name") or system_name
        values_by_id, ids_by_label, config_metas = _parse_all_configs(f)
        meta = FieldMeta(
            system_name=system_name,
            label=label,
            type_id=int(f.get("type_id") or 0),
            values_by_id=values_by_id,
            ids_by_label=ids_by_label,
            configs=config_metas,
        )
        by_label[_norm(label)] = meta
        # also index under the plain `name` key
        by_label[_norm(f.get("name") or "")] = meta
        by_system[system_name] = meta

    type_label_to_id = {_norm(t["name"]): int(t["id"]) for t in types}
    priority_label_to_id = {_norm(p["name"]): int(p["id"]) for p in prios}
    for p in prios:
        short = p.get("short_name") or p.get("name")
        priority_label_to_id.setdefault(_norm(short), int(p["id"]))
    priority_id_to_label = {int(p["id"]): p.get("name", str(p["id"])) for p in prios}

    return {
        "fields_by_label":      by_label,
        "fields_by_system":     by_system,
        "type_label_to_id":     type_label_to_id,
        "priority_label_to_id": priority_label_to_id,
        "priority_id_to_label": priority_id_to_label,
    }


def get_registry() -> FieldRegistry:
    raw = _build_registry_raw()
    return FieldRegistry(**raw)
