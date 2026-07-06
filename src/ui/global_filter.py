"""Global scope + Business-Unit selector, shared by every tab.

One control bar (rendered once in app.py, between the header and the tab bar)
replaces the per-tab dropdowns that had each invented their own pattern:

    [ 🌐 Website · 📱 Mobile App · 🧩 Microservices ]   [ Business Unit ▾ ]

Tabs read the selection via `current()` — they never render their own scope/BU
widgets.  The BU list adapts to the chosen scope, and each scope remembers its
own last BU (per-scope widget key), so switching back and forth never produces
an invalid selection.

All-BU overview sections (Overview, Report, the Backlog summary table) are
cross-BU comparisons by design and intentionally ignore the BU selection.
"""
from __future__ import annotations

import streamlit as st

from ..bu_rules import ALL_RULES

# Canonical display order for website BUs (matches the historical Explorer
# ordering); BUs not listed fall to the end alphabetically.
_BU_ORDER = [
    "Drogas", "ICI Paris XL", "Kruidvat", "Marionnaud", "Savers",
    "Superdrug", "The Perfume Shop", "Trekpleister", "Watsons",
]

_SCOPE_LABELS = {
    "website":    "🌐 Website",
    "mobile_app": "📱 Mobile App",
    "next_gen":   "🧩 Microservices",
}


def scopes_available() -> list[str]:
    present = {r.scope for r in ALL_RULES}
    return [s for s in ("website", "mobile_app", "next_gen") if s in present]


def bus_for_scope(scope: str) -> list[str]:
    bus = {r.bu for r in ALL_RULES if r.scope == scope}
    ordered = [b for b in _BU_ORDER if b in bus]
    return ordered + sorted(b for b in bus if b not in ordered)


def render() -> tuple[str, str]:
    """Render the global control bar; returns (scope, bu)."""
    scopes = scopes_available()
    labels = [_SCOPE_LABELS[s] for s in scopes]
    with st.container(key="global_filter"):
        c1, c2 = st.columns([2.2, 3], vertical_alignment="center")
        chosen = c1.radio("Scope", labels, horizontal=True,
                          key="global_scope", label_visibility="collapsed")
        scope = scopes[labels.index(chosen)]
        bus = bus_for_scope(scope)
        if bus:
            # Per-scope key: each scope remembers its own BU, and a BU that
            # doesn't exist in the new scope can never be selected.
            c2.selectbox("Business Unit", bus,
                         key=f"global_bu_{scope}", label_visibility="collapsed")
        else:
            c2.caption("No Business Units in this scope.")
    return current()


def current() -> tuple[str, str]:
    """The active (scope, bu) — safe to call from any tab / fragment.

    Falls back to the first available scope/BU when the bar hasn't rendered
    yet (or a stale session value no longer exists)."""
    scopes = scopes_available()
    by_label = {_SCOPE_LABELS[s]: s for s in scopes}
    scope = by_label.get(st.session_state.get("global_scope"),
                         scopes[0] if scopes else "website")
    bus = bus_for_scope(scope)
    bu = st.session_state.get(f"global_bu_{scope}")
    if bu not in bus:
        bu = bus[0] if bus else ""
    return scope, bu


def scope_label(scope: str) -> str:
    return _SCOPE_LABELS.get(scope, scope)
