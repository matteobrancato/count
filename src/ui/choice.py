"""One way to pick one of a few options: a row of radio buttons.

The page used to mix two controls for the same job — a radio in Coverage, and
button-shaped segmented controls everywhere else (scope, run, granularity,
release kind).  Two looks for one gesture, so a reader had to work out each time
whether the buttons were filters, actions or tabs.  Every pick-one control now
goes through here, and `styles.py` draws the radio once for all of them.

A dropdown stays where the list is long (releases, incidents), a checkbox or a
multiselect where several can be on at once; a few options of which exactly one
applies are always a radio.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import streamlit as st


def pick(label: str, options: Sequence[Any], *, key: str,
         format_func: Callable[[Any], Any] = str, index: int = 0) -> Any:
    """The option picked, as the caller passed it — never its display text.

    Always one option on: a radio cannot be cleared, so a click on the active one
    changes nothing.  *label* is kept for screen readers; the page names each
    control with its own text beside it.  The option at *index* is on until the
    reader picks — or until the caller seeds `st.session_state[key]`, in which
    case *index* is left at 0 (Streamlit warns when a widget has both a default
    and a value set through the Session State API).
    """
    return st.radio(label, options, index=index, horizontal=True, key=key,
                    format_func=format_func, label_visibility="collapsed")
