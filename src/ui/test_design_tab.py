"""AI Test Design tab (Beta) — sources in, test cases traced to their AC out.

The whole tab is ONE fragment.  Streamlit runs every tab's body on every
script run, and a widget interaction reruns the script: without the fragment,
typing notes or dropping a file would recompute the entire dashboard behind it.
Inside it, only this section reruns.  And nothing here calls Jira, Confluence
or Gemini until "Generate" is pressed, so the tab costs the dashboard nothing.
"""
from __future__ import annotations

import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pandas as pd
import streamlit as st

from .. import confluence_client, gemini_client, jira_client
from .. import test_design as td
from .styles import COLORS, section_title

try:
    from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
except Exception:                                                       # noqa: BLE001
    add_script_run_ctx = get_script_run_ctx = None

_RESULT_KEY = "td_result"


def _md(text: str) -> str:
    """Model-written text shown through markdown: shown literally, never parsed.

    A title such as "User enters <invalid code>" would otherwise lose its
    placeholder as an HTML tag, and a stray `*` would restyle the line.
    """
    return re.sub(r"([\\`*_{}\[\]<>#|~])", r"\\\1", text or "")


def _chip(text: str, bg: str, fg: str) -> str:
    return (f"<span style='display:inline-block;padding:1px 9px;margin:0 6px 4px 0;"
            f"border-radius:999px;background:{bg};color:{fg};font-size:12px;"
            f"font-weight:600'>{html.escape(text)}</span>")


_PRIORITY_COLOURS = {
    "Highest": ("#FDECEC", COLORS["danger"]),
    "High":    ("#FEF6E7", "#B45309"),
    "Medium":  (COLORS["brand_soft"], COLORS["brand_strong"]),
    "Low":     (COLORS["grid"], COLORS["muted"]),
}


def _header() -> None:
    section_title("AI Test Design " + _chip("Beta", COLORS["brand_soft"], COLORS["brand"]),
                  top=2)
    st.caption(
        "Turns Jira stories, Confluence pages and documents into TestRail-style "
        "test cases — the fewest that cover every acceptance criterion, step by "
        "step. Always review before use.")
    st.caption(
        "🔒 Everything you add here is sent to Google Gemini to generate the "
        "tests. Don't add personal data or content not cleared for external AI "
        "services.")


def _gather(keys: list[str], refs: list[str]) -> tuple[list[dict], list[dict]]:
    """Stories and pages fetched in parallel, returned in the order written."""
    jobs = ([(jira_client.fetch_story, k) for k in keys]
            + [(confluence_client.fetch_page, r) for r in refs])
    if not jobs:
        return [], []
    ctx = get_script_run_ctx() if get_script_run_ctx else None

    def _attach() -> None:
        # The fetchers are st.cache_data functions: give the worker threads the
        # script context so the cache is shared instead of warning and bypassed.
        if ctx is not None and add_script_run_ctx is not None:
            add_script_run_ctx(threading.current_thread(), ctx)

    with ThreadPoolExecutor(max_workers=min(len(jobs), 6), initializer=_attach) as pool:
        results = list(pool.map(lambda job: job[0](job[1]), jobs))
    return results[:len(keys)], results[len(keys):]


def _run(jira_txt: str, conf_txt: str, files: list, notes: str,
         device: str, negative: bool) -> None:
    keys = jira_client.extract_issue_keys(jira_txt)
    refs = confluence_client.extract_page_refs(conf_txt)
    if not (keys or refs or files or notes.strip()):
        st.warning("Add at least one source: a Jira story, a Confluence page, "
                   "a file or some notes.")
        return
    if len(files) > td.MAX_FILES:
        st.warning(f"Up to {td.MAX_FILES} files at a time — remove "
                   f"{len(files) - td.MAX_FILES}.")
        return
    total = sum(f.size for f in files)
    if total > td.MAX_TOTAL_BYTES:
        st.warning(f"The files add up to {total / 1e6:.1f} MB; the limit is "
                   f"{td.MAX_TOTAL_BYTES / 1e6:.0f} MB.")
        return

    started = time.time()
    with st.status("Reading the sources…", expanded=False) as status:
        stories, pages = _gather(keys, refs)
        sources = ([td.story_source(s) for s in stories]
                   + [td.page_source(p) for p in pages]
                   + [td.read_file(f.name, f.getvalue()) for f in files])
        if (note := td.notes_source(notes)) is not None:
            sources.append(note)

        usable = sum(s.ok for s in sources)
        if usable:
            status.update(label=f"Writing test cases from {usable} source(s)…")
            cooling = st.session_state.setdefault(gemini_client.COOLDOWN_KEY, {})
            outcome = td.generate(sources, device, negative, cooling)
        else:
            outcome = td.Outcome(None, None, "None of the sources could be read.")
        status.update(
            label=("Test cases ready" if outcome.design else "No test cases generated"),
            state=("complete" if outcome.design else "error"))

    # Uploaded files stay out of session state: the design is what is kept.
    st.session_state[_RESULT_KEY] = {
        "sources": [replace(s, blob=None, text="") for s in sources],
        "outcome": outcome,
        "seconds": time.time() - started,
    }


def _render_sources(sources: list[td.Source]) -> None:
    lines = []
    for s in sources:
        icon = "❌" if not s.ok else ("⚠️" if s.warn else "✅")
        lines.append(f"{icon} **{_md(s.label)}** — {_md(s.note)}")
    st.markdown("  \n".join(lines))


def _render_result(result: dict) -> None:
    outcome: td.Outcome = result["outcome"]
    design = outcome.design

    section_title("What the AI read", top=14)
    _render_sources(result["sources"])

    if design is None:
        st.error(outcome.error)
        if outcome.raw:
            with st.expander("Model answer"):
                st.code(outcome.raw[:8000], language="json", wrap_lines=True)
        return

    section_title(f"{len(design.criteria)} acceptance criteria → "
                  f"{len(design.tests)} test case{'s' * (len(design.tests) != 1)}",
                  top=14)
    st.caption(f"Generated by `{outcome.model}` in {result['seconds']:.0f}s.")

    if design.uncovered:
        st.warning("Not covered by any test case: " + ", ".join(design.uncovered))
    if design.dropped:
        st.caption(f"{design.dropped} incomplete test case(s) returned without "
                   "steps were discarded.")

    if design.criteria:
        st.table(pd.DataFrame(
            [{"Acceptance criterion": c["text"],
              "Source": c["source"],
              "Test cases": ", ".join(design.tests_for(c["id"])) or "not covered"}
             for c in design.criteria],
            index=pd.Index([c["id"] for c in design.criteria], name="AC")))

    for i, t in enumerate(design.tests):
        with st.expander(f"**{t.id}** · {_md(t.title)}", expanded=(i == 0)):
            bg, fg = _PRIORITY_COLOURS.get(t.priority, _PRIORITY_COLOURS["Medium"])
            chips = (_chip(t.priority, bg, fg)
                     + _chip(t.type, COLORS["grid"], COLORS["text"])
                     + _chip(t.device, COLORS["grid"], COLORS["text"])
                     + "".join(_chip(a, COLORS["brand_soft"], COLORS["brand"])
                               for a in t.covers))
            st.markdown(chips, unsafe_allow_html=True)
            st.markdown(f"**Preconditions** — {_md(t.preconditions) or '—'}")
            st.table(pd.DataFrame(
                [{"Step": step, "Expected result": expected or "—"}
                 for step, expected in t.steps],
                index=pd.RangeIndex(1, len(t.steps) + 1, name="#")))

    if design.questions:
        section_title("To clarify with the Product Owner", top=14)
        st.markdown("\n".join(f"- {_md(q)}" for q in design.questions))

    with st.expander("Copy as text"):
        st.code(td.to_text(design), language=None, wrap_lines=True)

    if st.button("Clear result", type="tertiary", key="td_clear"):
        st.session_state.pop(_RESULT_KEY, None)
        st.rerun(scope="fragment")


@st.fragment
def render() -> None:
    _header()
    if not gemini_client.ready():
        st.info("AI Test Design needs a Gemini API key — add `GEMINI_API_KEY` "
                "to the app secrets.")
        return

    jira_ok = jira_client.available()
    conf_ok = confluence_client.available()
    with st.form("td_form", border=True):
        left, right = st.columns(2, gap="medium")
        jira_txt = left.text_area(
            "Jira stories", height=88, disabled=not jira_ok,
            placeholder=("IPXL20-15740, https://…/browse/SD-512" if jira_ok
                         else "Jira is not configured for this app"))
        conf_txt = right.text_area(
            "Confluence pages", height=88, disabled=not conf_ok,
            placeholder=("https://…/wiki/spaces/…/pages/123456" if conf_ok
                         else "Confluence is not configured for this app"))
        # The limits go in the label: the uploader's own caption shows the
        # server's per-file maximum, which is not what the request allows.
        files = st.file_uploader(
            f"Documents & images · up to {td.MAX_FILES} files, "
            f"{td.MAX_TOTAL_BYTES // (1024 * 1024)} MB in total",
            type=td.UPLOAD_TYPES, accept_multiple_files=True)
        notes = st.text_area(
            "Notes", height=88,
            placeholder="Paste acceptance criteria, or tell the AI what to focus on…")
        opt_device, opt_negative, opt_go = st.columns(
            [1.4, 1.5, 1.1], vertical_alignment="bottom")
        device = opt_device.radio("Device", td.DEVICES, horizontal=True)
        negative = opt_negative.toggle("Include negative & edge cases")
        go = opt_go.form_submit_button("✨ Generate test cases", type="primary",
                                       width="stretch")

    if go:
        _run(jira_txt if jira_ok else "", conf_txt if conf_ok else "",
             list(files or []), notes or "", device, negative)

    if (result := st.session_state.get(_RESULT_KEY)) is not None:
        _render_result(result)
