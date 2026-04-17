"""Debug tab — inspect raw TestRail fields, values, and sample cases.

Helps diagnose mismatches between bu_rules field labels and actual TestRail config.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from .. import testrail_client as tr
from ..field_resolver import get_registry


def render() -> None:
    st.subheader("🔧 Debug: TestRail Field Inspector")

    reg = get_registry()

    # ---- Show all custom fields the registry resolved
    st.markdown("### Custom fields resolved from TestRail")
    rows = []
    for norm_label, meta in sorted(reg.fields_by_label.items()):
        vals = ", ".join(f"{k}={v}" for k, v in sorted(meta.values_by_id.items())[:15])
        rows.append({
            "Normalised label": norm_label,
            "System name": meta.system_name,
            "Type ID": meta.type_id,
            "Values (first 15)": vals,
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ---- Show case types and priorities
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("### Case Types")
        st.json({k: v for k, v in sorted(reg.type_label_to_id.items())})
    with c2:
        st.markdown("### Priorities")
        st.json({k: v for k, v in sorted(reg.priority_label_to_id.items())})

    # ---- Sample cases from any suite
    st.divider()
    st.markdown("### Sample raw cases")
    suite_id = st.number_input("Suite ID", value=722, step=1)
    n_cases = st.slider("Number of cases to show", 1, 50, 5)

    if st.button("Fetch sample"):
        try:
            pid = tr.resolve_project_id(int(suite_id))
            st.info(f"Suite {suite_id} → project {pid}")
            cases = tr.fetch_cases(pid, int(suite_id))
            st.success(f"Total cases in suite: {len(cases)}")
            for c in cases[:n_cases]:
                with st.expander(f"C{c['id']}: {c.get('title', '(no title)')[:80]}"):
                    # Show only the custom_ fields + key builtins
                    display = {k: v for k, v in c.items() if k.startswith("custom_") or k in (
                        "id", "title", "type_id", "priority_id", "section_id"
                    )}
                    st.json(display)
        except Exception as exc:
            st.error(str(exc))
