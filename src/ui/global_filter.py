"""Global scope selector, and the Business Unit last opened, shared by every tab.

One control, drawn once by app.py inside the top bar next to the title:

    [ 🌐 Website | 📱 Mobile App | 🧩 Microservices | 🎯 Martech ]

The Business Unit is no longer picked here.  It is picked where the BUs are
listed — a click on a row of the Backlog table opens that BU's window — and the
choice is kept in session state, one per scope, so the next visit to the same
scope still knows which BU was last opened.  Sections read it via `current()`;
none draws its own scope or BU widget.

All-BU sections (the Backlog table, the Leakage table of every BU) are
cross-BU comparisons by design and ignore the BU selection.
"""
from __future__ import annotations

import streamlit as st

from .. import martech
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
    "martech":    "🎯 Martech",
}


def scopes_available() -> list[str]:
    """The scopes the rules define, then Martech once its API key is in the
    secrets.  Martech has no rules: its data is one plan on another TestRail
    instance (see `martech`)."""
    present = {r.scope for r in ALL_RULES}
    scopes = [s for s in ("website", "mobile_app", "next_gen") if s in present]
    return scopes + (["martech"] if martech.configured() else [])


def bus_for_scope(scope: str) -> list[str]:
    if scope == "martech":
        return [b for b in _BU_ORDER if b in martech.KNOWN_BUS]
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
    is always a link to what the user is looking at (copy-paste and send).  With
    no BU opened yet the link carries the scope alone."""
    try:
        params = st.query_params
        if params.get("scope") != scope:
            params["scope"] = scope
        if bu:
            if params.get("bu") != bu:
                params["bu"] = bu
        elif "bu" in params:
            del params["bu"]
    except Exception:                                                   # noqa: BLE001
        pass                      # the URL is a convenience; the selection stands


def render() -> tuple[str, str]:
    """Draw the scope control where the caller is (app.py places it in the top
    bar); returns (scope, bu), the BU being the one last opened in this scope.

    A segmented control rather than a radio: the same choices in a third of the
    height, and it reads as the page's main switch.  `required` keeps one scope
    always selected — a click on the active one cannot clear it.
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
    scope, bu = current()
    _publish_to_url(scope, opened(scope))
    return scope, bu


def opened(scope: str) -> str:
    """The BU last opened in *scope*, or "" when none has been."""
    return str(st.session_state.get(f"global_bu_{scope}") or "")


def select_bu(scope: str, bu: str) -> None:
    """Record *bu* as the one opened in *scope*.  A plain session value, not a
    widget's: no control is left to own it, so it outlives a visit to another
    scope.  The address bar follows at once — this runs inside a fragment, and
    the top bar that normally publishes it does not."""
    st.session_state[f"global_bu_{scope}"] = bu
    _publish_to_url(scope, bu)


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
    if scope == "martech":
        # The BUs are the plan's: a run this module does not know is a row of
        # its own, so no fixed list can say what is valid.
        return scope, str(bu or "")
    if bu not in bus:
        bu = bus[0] if bus else ""
    return scope, bu


def scope_label(scope: str) -> str:
    return _SCOPE_LABELS.get(scope, scope)
