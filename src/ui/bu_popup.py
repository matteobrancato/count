"""The Business Unit window.

A click on a BU in the Backlog table opens a window with everything specific to
that BU — Detail, Coverage, Leakage and Trend — instead of a tab bar that
repeated the BU picked somewhere else.  The table stays where it is, behind the
window, and closing the window (✕, Esc, a click outside) leaves it untouched.

Two pieces:

* `clicked()` listens for the click.  A Streamlit element cannot be bound to a
  callback from custom HTML, so one invisible component attaches a single click
  handler to the document, and any row of a clickable `.bl-summary` table that
  carries `data-bu` reports its BU.  The handler is attached once and survives
  every re-render of the table — it never needs to find the new DOM.
* `open_for()` draws the window.  It is an `st.dialog`, so what happens inside
  it reruns the window and nothing else; the tabs inside are lazy, so only the
  open one runs (the same rule as the page's own tab bar).
"""
from __future__ import annotations

import html

import streamlit as st

from . import global_filter, isolation

_CLICK_JS = """
export default function (component) {
  const { setTriggerValue } = component;
  const onClick = (event) => {
    const row = event.target.closest('.bl-summary.bl-clickable tr[data-bu]');
    if (!row) return;
    event.preventDefault();
    setTriggerValue('bu', row.dataset.bu);
  };
  document.addEventListener('click', onClick);
  return () => document.removeEventListener('click', onClick);
}
"""
# isolate_styles=False: the handler lives on the page's own document, where the
# table is, not in a shadow root of its own.
_bu_clicks = st.components.v2.component("count_bu_clicks", js=_CLICK_JS,
                                        isolate_styles=False)


# The markup the handler above reads.  Both tables that open a window (the Backlog
# and Martech) build their rows with these, so the contract — `bl-clickable` on
# the table, `data-bu` on the row — lives in one place.
def row_attr(bu: str, clickable: bool) -> str:
    """The attribute the click handler reads off a table row."""
    return f' data-bu="{html.escape(bu, quote=True)}"' if clickable else ""


def name_cell(bu: str, clickable: bool, sub: str = "") -> str:
    """A table's first cell: the BU's name, as a button when the row opens a
    window.  The name is brand-coloured with a chevron that moves on hover —
    "this opens something" has to be readable without a hint.  *sub* is a small
    second line (Martech says how many runs the BU has)."""
    name = html.escape(bu)
    extra = f'<span class="sub">{html.escape(sub)}</span>' if sub else ""
    if not clickable:
        return f'<td class="l bu">{name}{extra}</td>'
    return ('<td class="l bu"><button type="button" class="bu-link" '
            f'data-bu="{html.escape(bu, quote=True)}" aria-haspopup="dialog">'
            f'<span class="bu-name">{name}</span><span class="bu-go">›</span>'
            f'</button>{extra}</td>')


def _ignore() -> None:
    """A trigger value reaches Python only if a callback is registered for it."""


def clicked() -> str | None:
    """The Business Unit whose row was clicked on this run, else None.  A click
    is reported once: the next run, whatever caused it, sees None."""
    event = _bu_clicks(key="bu_clicks", on_bu_change=_ignore)
    return getattr(event, "bu", None) or None


def _sections() -> list[tuple[str, object, str]]:
    # Imported here: the tabs import this module for `clicked`, so importing
    # them at the top would be a cycle.
    from . import backlog_tab, coverage_tab, leakage_tab
    return [
        ("🔎 Detail", lambda: backlog_tab.render_detail(popup=True), "detail_anim"),
        ("📐 Coverage", coverage_tab.render, ""),
        ("🐞 Leakage", leakage_tab.render, ""),
        ("📈 Trend", leakage_tab.render_trend, ""),
    ]


def _body(scope: str, bu: str) -> None:
    if scope == "martech":
        from . import martech_tab
        isolation.render_isolated(lambda: martech_tab.render_bu(bu),
                                  f"Martech · {bu}", "martech_bu_anim")
        return
    sections = _sections()
    # One key for every BU: the tab chosen stays chosen while the reader goes
    # from one BU to the next, which is what comparing them looks like.
    tabs = st.tabs([label for label, _fn, _anim in sections],
                   key="bu_popup_tabs", on_change="rerun")
    opened = next((i for i, t in enumerate(tabs) if t.open), 0)
    label, render_fn, anim_key = sections[opened]
    with tabs[opened]:
        isolation.render_isolated(render_fn, f"{label} · {bu}", anim_key)


def open_for(scope: str, bu: str) -> None:
    """Open the window for *bu* in *scope*.  Only one dialog may be drawn per
    run, which holds: the Overview dialog is opened by a button, never on the
    run a table row was clicked."""
    title = bu if scope == "website" else f"{bu} · {global_filter.scope_label(scope)}"
    st.dialog(title, width="large")(_body)(scope, bu)
