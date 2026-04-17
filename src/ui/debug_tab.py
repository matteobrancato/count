"""Debug tab — inspect raw TestRail data to diagnose field mapping and count mismatches."""
from __future__ import annotations

import re
import traceback

import pandas as pd
import streamlit as st

from .. import testrail_client as tr
from ..bu_rules import ALL_RULES
from ..field_resolver import get_registry
from ..rules_engine import (
    _is_deprecated, _get_multi_countries, _get_prod_sanity,
    _devices_for, _rule_matches,
)


def _parse_items(raw_items: str) -> list[dict]:
    out = []
    for line in (raw_items or "").splitlines():
        m = re.match(r"^(\d+)\s*,\s*(.+)$", line.strip())
        if m:
            out.append({"id": int(m.group(1)), "label": m.group(2).strip()})
    return out


def render() -> None:
    st.subheader("🔧 Debug — TestRail Inspector")

    # ============================================================ 1. All custom fields
    st.markdown("### 1. Custom fields from `get_case_fields`")
    try:
        raw_fields = tr.fetch_case_fields()
        rows = []
        for f in raw_fields:
            label  = f.get("label") or f.get("name")
            system = f.get("system_name") or f.get("name")
            items_all: list[dict] = []
            for cfg in (f.get("configs") or []):
                items_all += _parse_items((cfg.get("options") or {}).get("items", ""))
            rows.append({
                "Label":       label,
                "system_name": system,
                "type_id":     f.get("type_id"),
                "Dropdown values": ", ".join(f"{x['id']}={x['label']}" for x in items_all) or "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(f"fetch_case_fields error: {exc}")
        st.code(traceback.format_exc())

    # ============================================================ 2. Resolver test
    st.markdown("---")
    st.markdown("### 2. Field resolver")
    label_input = st.text_input("Label to resolve", value="Automation Status Testim Desktop", key="dbg_lbl")
    try:
        reg = get_registry()
        meta = reg.field(label_input)
        if meta:
            st.success(f"system_name → `{meta.system_name}`  (type_id={meta.type_id})")
            df_vals = pd.DataFrame([{"id": k, "label": v} for k, v in sorted(meta.values_by_id.items())])
            if not df_vals.empty:
                st.dataframe(df_vals, use_container_width=True, hide_index=True)
            else:
                st.info("No dropdown values (field is likely Checkbox or Multi-select — values resolved differently).")
        else:
            st.error(f"`{label_input}` not found. Known labels (first 80):")
            st.code("\n".join(sorted(reg.fields_by_label.keys())[:80]))
    except Exception as exc:
        st.error(str(exc))
        st.code(traceback.format_exc())

    # ============================================================ 3. Raw cases
    st.markdown("---")
    st.markdown("### 3. Raw cases from a suite")

    suite_opts = sorted({r.suite_id for r in ALL_RULES})
    suite_labels = {}
    for r in ALL_RULES:
        suite_labels.setdefault(r.suite_id, f"Suite {r.suite_id}")
        suite_labels[r.suite_id] = f"{r.suite_id} — {r.bu} / {r.scope}"

    chosen_suite = st.selectbox("Suite", suite_opts,
                                format_func=lambda s: suite_labels.get(s, str(s)),
                                key="dbg_suite")

    if st.button("📥 Load cases", key="dbg_load"):
        with st.spinner("Downloading…"):
            try:
                pid   = tr.resolve_project_id(chosen_suite)
                cases = tr.fetch_cases(pid, chosen_suite)
                st.session_state["dbg_cases"]    = cases
                st.session_state["dbg_suite_id"] = chosen_suite
                st.success(f"Loaded **{len(cases)}** cases (project {pid})")
            except Exception as exc:
                st.error(str(exc))
                st.code(traceback.format_exc())

    cases: list[dict] = st.session_state.get("dbg_cases", [])
    loaded_suite: int | None = st.session_state.get("dbg_suite_id")

    if not cases:
        st.info("Click 'Load cases' to continue.")
        return

    st.caption(f"{len(cases)} cases loaded from suite {loaded_suite}")

    all_keys    = sorted({k for c in cases for k in c})
    custom_keys = [k for k in all_keys if k.startswith("custom_")]
    base_keys   = ["id", "title", "type_id", "priority_id", "section_id"]
    st.markdown("**All field keys on cases:**")
    st.code(" | ".join(all_keys))

    show_cols = st.multiselect("Columns to show", base_keys + custom_keys,
                               default=base_keys + custom_keys[:12], key="dbg_cols")
    max_rows  = st.slider("Max rows", 10, 500, 50, key="dbg_nrows")
    if show_cols:
        df_raw = pd.DataFrame(cases[:max_rows])
        avail  = [c for c in show_cols if c in df_raw.columns]
        st.dataframe(df_raw[avail], use_container_width=True, hide_index=True)

    # ============================================================ 4. Rule dry-run
    st.markdown("---")
    st.markdown("### 4. Rule dry-run on loaded suite")

    relevant = [r for r in ALL_RULES if r.suite_id == loaded_suite]
    if not relevant:
        st.warning(f"No rules defined for suite {loaded_suite}.")
        return

    try:
        reg = get_registry()
    except Exception as exc:
        st.error(str(exc))
        st.code(traceback.format_exc())
        return

    summary: list[dict] = []
    match_cache: dict[str, list[dict]] = {}

    for rule in relevant:
        ok = fail_type = fail_dep = fail_status = fail_country = no_field = 0
        matched_rows = []
        status_meta  = reg.field(rule.status_field_label)
        allowed_ids  = (reg.status_value_ids(rule.status_field_label, rule.automated_values)
                        if status_meta else set())
        expected_type_ids = (
            {reg.type_id(t) for t in rule.type_filter} - {None}
            if rule.type_filter else set()
        )

        for case in cases:
            # type
            if expected_type_ids and case.get("type_id") not in expected_type_ids:
                fail_type += 1; continue
            # deprecated
            if _is_deprecated(case, reg):
                fail_dep += 1; continue
            # status field
            if not status_meta:
                no_field += 1; continue
            raw_st = case.get(status_meta.system_name)
            passed = False
            if isinstance(raw_st, list):
                passed = any(int(v) in allowed_ids for v in raw_st if str(v).isdigit())
            elif isinstance(raw_st, int):
                passed = raw_st in allowed_ids
            elif isinstance(raw_st, str) and raw_st.strip().isdigit():
                passed = int(raw_st.strip()) in allowed_ids
            if not passed:
                fail_status += 1; continue
            # country
            if rule.countries_filter:
                tokens = set(_get_multi_countries(case, reg))
                if not any(c in tokens for c in rule.countries_filter):
                    fail_country += 1; continue
            ok += 1
            st_lbl = status_meta.values_by_id.get(raw_st) if isinstance(raw_st, int) else str(raw_st)
            matched_rows.append({
                "id":               case["id"],
                "title":            (case.get("title") or "")[:70],
                "status":           st_lbl,
                "multi_countries":  _get_multi_countries(case, reg),
                "device":           _devices_for(case, reg),
                "priority_id":      case.get("priority_id"),
                "prod_sanity":      _get_prod_sanity(case, reg),
            })

        match_cache[rule.name] = matched_rows
        summary.append({
            "Rule":          rule.name,
            "status_field":  rule.status_field_label,
            "field_found":   "✅" if status_meta else f"❌ NOT FOUND",
            "allowed_ids":   str(sorted(allowed_ids)),
            "countries":     ", ".join(rule.countries_filter) or "(none)",
            "✅ Match":      ok,
            "❌ Type":       fail_type,
            "❌ Deprecated": fail_dep,
            "❌ Status":     fail_status,
            "❌ Country":    fail_country,
            "❌ No field":   no_field,
        })

    st.dataframe(pd.DataFrame(summary), use_container_width=True, hide_index=True)

    # Drill-down
    drill = st.selectbox("Drill into rule", [r.name for r in relevant], key="dbg_drill")
    matched = match_cache.get(drill, [])
    st.caption(f"**{len(matched)}** matched cases for `{drill}`")
    if matched:
        st.dataframe(pd.DataFrame(matched), use_container_width=True, hide_index=True)

    # Value distribution for the status field
    drill_rule = next((r for r in relevant if r.name == drill), None)
    if drill_rule:
        smeta = reg.field(drill_rule.status_field_label)
        if smeta:
            st.markdown(f"#### All values of `{drill_rule.status_field_label}` on these cases")
            vc: dict[str, int] = {}
            for c in cases:
                raw = c.get(smeta.system_name)
                if isinstance(raw, int):
                    lbl = f"id={raw} → {smeta.values_by_id.get(raw, '?')}"
                else:
                    lbl = str(raw)
                vc[lbl] = vc.get(lbl, 0) + 1
            st.dataframe(
                pd.DataFrame(list(vc.items()), columns=["value", "count"])
                  .sort_values("count", ascending=False),
                use_container_width=True, hide_index=True
            )
        else:
            st.error(f"Field `{drill_rule.status_field_label}` not found in registry!")

        # multi_countries tokens distribution
        st.markdown("#### `multi_countries` token distribution on ALL cases")
        mc_vc: dict[str, int] = {}
        for c in cases:
            for tok in _get_multi_countries(c, reg):
                mc_vc[tok] = mc_vc.get(tok, 0) + 1
        if mc_vc:
            st.dataframe(
                pd.DataFrame(list(mc_vc.items()), columns=["token", "count"])
                  .sort_values("count", ascending=False),
                use_container_width=True, hide_index=True
            )
        else:
            st.warning("No multi_countries values found — field label might be wrong.")

        # Deprecated & Prod Sanity stats
        st.markdown("#### Deprecated / Prod Sanity on ALL cases")
        dep_n  = sum(1 for c in cases if _is_deprecated(c, reg))
        prod_n = sum(1 for c in cases if _get_prod_sanity(c, reg))
        st.write(f"- Deprecated=True: **{dep_n}** / {len(cases)}")
        st.write(f"- Prod Sanity=True: **{prod_n}** / {len(cases)}")
