"""Run one section's renderer so that its failure stays in its own place.

Shared by the main tabs and the Business Unit window: each
used to need its own try/except, or a single failing section blanked everything
drawn after it.  Here the section catches its own failure, logs it with the
traceback and shows it where it happened — the rest of the page stays usable.
"""
from __future__ import annotations

import logging
import traceback

import streamlit as st

logger = logging.getLogger(__name__)


def render_isolated(render_fn, label: str, anim_key: str = "") -> None:
    """Call *render_fn*; an exception becomes an error box, not a blank page.

    *anim_key* wraps the section in a keyed container, the hook the fade-in
    animation in `styles.py` hangs on.  Streamlit's own control-flow exceptions
    (st.rerun, st.stop) are not `Exception`s and pass through untouched.
    """
    try:
        if anim_key:
            with st.container(key=anim_key):
                render_fn()
        else:
            render_fn()
    except Exception as exc:  # isolate, never cascade
        logger.exception("%s failed to render", label)
        st.error(f"⚠️ {label} could not be rendered: {exc}")
        with st.expander("Technical details"):
            st.code(traceback.format_exc())
