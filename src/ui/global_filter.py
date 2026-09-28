"""Global scope + Business-Unit selector, shared by every tab.

Two inline controls, drawn once by app.py inside the top bar next to the
title, replace the per-tab dropdowns that had each invented their own pattern:

    [ 🌐 Website | 📱 Mobile App | 🧩 Microservices ]  [ Business Unit ▾ ]

Tabs read the selection via `current()` — they never render their own scope/BU
widgets.  The BU list adapts to the chosen scope, and each scope remembers its
own last BU (per-scope widget key), so switching back and forth never produces
an invalid selection.

All-BU overview sections (Overview, the Backlog and Leakage tables) are
cross-BU comparisons by design and intentionally ignore the BU selection.
"""
from __future__ import annotations

import streamlit as st

from ..bu_rules import ALL_RULES

# Canonical display order for website BUs (the order the team reads them in,
# ordering); BUs not listed fall to the end alphabetically.
_BU_ORDER = [
    "Drogas", "ICI Paris XL", "Kruidvat", "Marionnaud", "Savers",
    "Superdrug", "The Perfume Shop", "Trekpleister", "Watsons Turkey", "Watsons Ukraine",
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


def _seed_from_url() -> None:
    """On the FIRST render of a session, adopt ?scope=&bu= from the URL.

    This is what makes a link shareable: a colleague opening
    `…/?scope=website&bu=Drogas` lands on exactly that view.  Runs once per
    session (guarded), so it can never fight the user's own clicks afterwards.
    Unknown values are ignored rather than erroring.
    """
    if st.session_state.get("_url_seeded"):
        return
    st.session_state["_url_seeded"] = True
    try:
        params = st.query_params
        scope = params.get("scope")
        if scope in _SCOPE_LABELS and scope in scopes_available():
            st.session_state["global_scope"] = _SCOPE_LABELS[scope]
        else:
            scope = None
        bu = params.get("bu")
        target = scope or current()[0]
        if bu and bu in bus_for_scope(target):
            st.session_state[f"global_bu_{target}"] = bu
    except Exception:                                                   # noqa: BLE001
        pass                      # a malformed URL must never block the app


def _publish_to_url(scope: str, bu: str) -> None:
    """Keep ?scope=&bu= in sync with the current selection, so the address bar
    is always a link to what the user is looking at (copy-paste and send)."""
    try:
        params = st.query_params
        if params.get("scope") != scope or params.get("bu") != bu:
            params["scope"] = scope
            if bu:
                params["bu"] = bu
    except Exception:                                                   # noqa: BLE001
        pass                      # the URL is a convenience; the selection stands


def render() -> tuple[str, str]:
    """Draw the scope and BU controls where the caller is (app.py places them
    in the top bar); returns (scope, bu).

    A segmented control rather than a radio: the same three choices in a third
    of the height, and it reads as the page's main switch.  `required` keeps
    one scope always selected — a click on the active one cannot clear it.
    """
    _seed_from_url()
    scopes = scopes_available()
    labels = [_SCOPE_LABELS[s] for s in scopes]
    # Seeded here, not passed as `default`: the URL seed may already have set
    # the key, and a widget given both raises a warning on every run.
    if st.session_state.get("global_scope") not in labels:
        st.session_state["global_scope"] = labels[0]
    st.segmented_control("Scope", labels, key="global_scope", required=True,
                         label_visibility="collapsed", width="content")
    scope = current()[0]
    bus = bus_for_scope(scope)
    if bus:
        # Per-scope key: each scope remembers its own BU, and a BU that
        # doesn't exist in the new scope can never be selected.
        st.selectbox("Business Unit", bus, key=f"global_bu_{scope}",
                     label_visibility="collapsed", width=220)
    else:
        st.caption("No Business Units in this scope.")
    scope, bu = current()
    _publish_to_url(scope, bu)
    return scope, bu


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
