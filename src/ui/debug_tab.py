"""Debug tab — inspect raw TestRail data to diagnose field mapping and count mismatches.

Sections:
  1. All custom fields → system_name + parsed dropdown values
  2. Field resolver test (type a label, see what it resolves to)
  3. Raw cases from any suite + all field values
  4. Rule dry-run: step-by-step match trace per rule on the loaded suite
"""
from __future__ import annotations

import re
import traceback

import pandas as pd
import streamlit as st

from .. import testrail_client as tr
from ..bu_rules import ALL_RULES
from ..field_resolver import get_registry
from ..rules_engine import _is_deprecated, _get_multi_countries, _devices_for, _rule_matches


# ----------------------------------------------------------------- helpers
def _parse_items(raw_items: str) -> list[dict]:
    out = []
    for line in (raw_items or "").splitlines():
        m = re.match(r"^(\d+)\s*,\s*(.+)$", line.strip())
        if m:
            out.append({"id": int(m.group(1)), "label": m.group(2).strip()})
    return out


# ----------------------------------------------------------------- render
def render() -> None:
    st.subheader("🔧 Debug — TestRail Inspector")
    st.caption(
        "Usa questa tab per verificare che i field names e i dropdown IDs corrispondano "
        "a quello che TestRail restituisce davvero, e per fare un dry-run delle regole."
    )

    # ============================================================ Section 1
    st.markdown("---")
    st.markdown("### 1. Custom fields risolti da `get_case_fields`")
    try:
        raw_fields = tr.fetch_case_fields()
        rows = []
        for f in raw_fields:
            label = f.get("label") or f.get("name")
            system = f.get("system_name") or f.get("name")
            # parse dropdown values from configs
            items_all: list[dict] = []
            for cfg in (f.get("configs") or []):
                items_all += _parse_items((cfg.get("options") or {}).get("items", ""))
            rows.append({
                "Label": label,
                "system_name": system,
                "type_id": f.get("type_id"),
                "Dropdown values": ", ".join(f"{x['id']}={x['label']}" for x in items_all) or "—",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    except Exception as exc:
        st.error(f"Errore fetch_case_fields: {exc}")
        st.code(traceback.format_exc())

    # ============================================================ Section 2
    st.markdown("---")
    st.markdown("### 2. Field resolver — testa un label")
    label_input = st.text_input(
        "Label da risolvere (es. 'Automation Status Testim Desktop')",
        value="Automation Status Testim Desktop",
        key="dbg_label",
    )
    try:
        reg = get_registry()
        meta = reg.field(label_input)
        if meta:
            st.success(f"**system_name** → `{meta.system_name}`  (type_id={meta.type_id})")
            df_vals = pd.DataFrame(
                [{"id": k, "label": v} for k, v in sorted(meta.values_by_id.items())]
            )
            st.dataframe(df_vals, use_container_width=True, hide_index=True)
        else:
            st.error(
                f"Campo `{label_input}` non trovato. "
                f"Label noti (normalizzati, primi 80):\n" +
                "\n".join(sorted(reg.fields_by_label.keys())[:80])
            )
    except Exception as exc:
        st.error(str(exc))
        st.code(traceback.format_exc())

    # ============================================================ Section 3
    st.markdown("---")
    st.markdown("### 3. Raw cases da una suite")

    suite_opts = sorted({r.suite_id for r in ALL_RULES})
    suite_labels = {
        r.suite_id: f"{r.suite_id} — {r.bu} ({r.scope})"
        for r in ALL_RULES
    }
    chosen_suite = st.selectbox(
        "Suite",
        suite_opts,
        format_func=lambda s: suite_labels.get(s, str(s)),
        key="dbg_suite_sel",
    )

    if st.button("📥 Carica cases", key="dbg_load"):
        with st.spinner("Download in corso…"):
            try:
                pid = tr.resolve_project_id(chosen_suite)
                cases = tr.fetch_cases(pid, chosen_suite)
                st.session_state["dbg_cases"] = cases
                st.session_state["dbg_suite_id"] = chosen_suite
                st.success(f"Caricati **{len(cases)}** cases dalla suite {chosen_suite} (project {pid})")
            except Exception as exc:
                st.error(str(exc))
                st.code(traceback.format_exc())

    cases: list[dict] = st.session_state.get("dbg_cases", [])
    loaded_suite: int | None = st.session_state.get("dbg_suite_id")

    if not cases:
        st.info("Clicca 'Carica cases' per iniziare.")
        return

    st.caption(f"{len(cases)} cases caricati dalla suite {loaded_suite}")

    # All keys present on actual cases
    all_keys = sorted({k for c in cases for k in c})
    custom_keys = [k for k in all_keys if k.startswith("custom_")]
    base_keys = ["id", "title", "type_id", "priority_id", "section_id"]

    st.markdown("**Field keys presenti sui cases:**")
    st.code(" | ".join(all_keys))

    show_cols = st.multiselect(
        "Colonne da mostrare",
        base_keys + custom_keys,
        default=(base_keys + custom_keys[:10]),
        key="dbg_cols",
    )
    max_rows = st.slider("Numero massimo di righe", 10, 500, 50, key="dbg_nrows")

    if show_cols:
        df_raw = pd.DataFrame(cases[:max_rows])
        avail = [c for c in show_cols if c in df_raw.columns]
        st.dataframe(df_raw[avail], use_container_width=True, hide_index=True)

    # ============================================================ Section 4
    st.markdown("---")
    st.markdown("### 4. Dry-run regole — step-by-step")

    relevant_rules = [r for r in ALL_RULES if r.suite_id == loaded_suite]
    if not relevant_rules:
        st.warning(f"Nessuna regola definita per la suite {loaded_suite}.")
        return

    try:
        reg = get_registry()
    except Exception as exc:
        st.error(f"Errore nel FieldRegistry: {exc}")
        st.code(traceback.format_exc())
        return

    # Summary table: per ogni regola, quanti cases passano ogni step
    summary_rows = []
    match_cache: dict[str, list[dict]] = {}

    for rule in relevant_rules:
        ok = fail_type = fail_dep = fail_status = fail_country = 0
        matched_ids = []

        status_meta = reg.field(rule.status_field_label)
        allowed_ids = (
            reg.status_value_ids(rule.status_field_label, rule.automated_values)
            if status_meta else set()
        )
        expected_type_ids = (
            {reg.type_id(t) for t in rule.type_filter} - {None}
            if rule.type_filter else set()
        )

        for case in cases:
            # step 1: type
            if expected_type_ids and case.get("type_id") not in expected_type_ids:
                fail_type += 1
                continue
            # step 2: deprecated
            if _is_deprecated(case, reg) != (rule.deprecated.strip().lower() == "yes"):
                fail_dep += 1
                continue
            # step 3: status field
            if not status_meta:
                fail_status += 1
                continue
            raw_st = case.get(status_meta.system_name)
            passed_status = False
            if isinstance(raw_st, list):
                passed_status = any(int(v) in allowed_ids for v in raw_st if str(v).isdigit())
            elif isinstance(raw_st, int):
                passed_status = raw_st in allowed_ids
            elif isinstance(raw_st, str) and raw_st.strip().isdigit():
                passed_status = int(raw_st.strip()) in allowed_ids
            if not passed_status:
                fail_status += 1
                continue
            # step 4: country
            if rule.countries_filter:
                tokens = set(_get_multi_countries(case, reg))
                if not any(c in tokens for c in rule.countries_filter):
                    fail_country += 1
                    continue
            ok += 1
            raw_mc = _get_multi_countries(case, reg)
            raw_st_lbl = status_meta.values_by_id.get(raw_st) if isinstance(raw_st, int) else str(raw_st)
            matched_ids.append({
                "id": case["id"],
                "title": (case.get("title") or "")[:70],
                "status": raw_st_lbl,
                "multi_countries": raw_mc,
                "device": _devices_for(case, reg),
                "priority_id": case.get("priority_id"),
            })

        match_cache[rule.name] = matched_ids
        status_field_found = "✅" if status_meta else f"❌ '{rule.status_field_label}' not found"
        summary_rows.append({
            "Regola": rule.name,
            "status_field": rule.status_field_label,
            "field found": status_field_found,
            "allowed IDs": str(sorted(allowed_ids)),
            "countries_filter": ", ".join(rule.countries_filter) or "(none)",
            "✅ Match": ok,
            "❌ Type": fail_type,
            "❌ Deprecated": fail_dep,
            "❌ Status": fail_status,
            "❌ Country": fail_country,
        })

    st.dataframe(pd.DataFrame(summary_rows), use_container_width=True, hide_index=True)

    # Drill-down su una singola regola
    rule_choice = st.selectbox(
        "Drill-down regola",
        [r.name for r in relevant_rules],
        key="dbg_rule_drill",
    )
    matched = match_cache.get(rule_choice, [])
    st.caption(f"**{len(matched)}** cases corrispondenti alla regola `{rule_choice}`")

    if matched:
        st.dataframe(pd.DataFrame(matched), use_container_width=True, hide_index=True)

    # Unique values found for the status field on ALL cases (to spot label drift)
    chosen_rule_obj = next((r for r in relevant_rules if r.name == rule_choice), None)
    if chosen_rule_obj:
        st.markdown(f"#### Valori trovati su tutti i cases per `{chosen_rule_obj.status_field_label}`")
        try:
            meta = reg.field(chosen_rule_obj.status_field_label)
            if meta:
                raw_vals = [c.get(meta.system_name) for c in cases]
                val_counts: dict = {}
                for v in raw_vals:
                    key = f"{v} → {meta.values_by_id.get(v, '?')}" if isinstance(v, int) else str(v)
                    val_counts[key] = val_counts.get(key, 0) + 1
                vc_df = (
                    pd.DataFrame(list(val_counts.items()), columns=["value", "count"])
                    .sort_values("count", ascending=False)
                )
                st.dataframe(vc_df, use_container_width=True, hide_index=True)
            else:
                st.error(f"Campo `{chosen_rule_obj.status_field_label}` non trovato nel registry.")
        except Exception as exc:
            st.error(str(exc))

        # Unique multi_countries values
        st.markdown("#### Valori `multi_countries` trovati sui cases")
        try:
            mc_all: dict[str, int] = {}
            for c in cases:
                for tok in _get_multi_countries(c, reg):
                    mc_all[tok] = mc_all.get(tok, 0) + 1
            if mc_all:
                mc_df = (
                    pd.DataFrame(list(mc_all.items()), columns=["token", "count"])
                    .sort_values("count", ascending=False)
                )
                st.dataframe(mc_df, use_container_width=True, hide_index=True)
            else:
                st.warning("Nessun valore multi_countries trovato — il field potrebbe chiamarsi diversamente.")
        except Exception as exc:
            st.error(str(exc))
