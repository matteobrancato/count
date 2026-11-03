"""The space above the first control of a page.

Streamlit puts a 16px gap between the children of a column, and an element that
draws nothing is still a child.  Four of them sat above what the reader looks at —
two style sheets and Dexter's fixed button above the header, the freshness bar
pinned over the tab row, the click listener at the top of a tab — and the radios
were 42px below the tab underline for it.  Their gaps are cancelled in CSS; these
tests hold the selectors to the keys the code gives those elements, since a key
renamed on one side leaves the gap back with no error anywhere.
"""
from __future__ import annotations

import pathlib
import re

from src.ui import styles

ROOT = pathlib.Path(__file__).resolve().parent.parent
CSS = styles._css()


def _src(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class TestInvisibleElementsTakeNoGap:
    def test_the_two_style_sheets_and_the_fab_above_the_header_are_cancelled(self):
        assert ':has([data-testid="stMarkdown"] style)' in CSS
        assert ":has(> .st-key-ai_assistant_fab)" in CSS
        assert re.search(r"\.st-key-bu_clicks\s*\{\s*margin-bottom: -1rem !important;", CSS)

    def test_the_selectors_name_the_keys_the_code_uses(self):
        assert 'key="ai_assistant_fab"' in _src("src/ui/chat_assistant.py")
        assert 'key="bu_clicks"' in _src("src/ui/bu_popup.py")
        assert 'key="tabs_zone"' in _src("app.py")
        assert 'key="freshness"' in _src("app.py")

    def test_the_freshness_gap_is_cancelled_only_where_the_bar_is_pinned(self):
        """Narrow, the bar is in the flow and its wrapper has a height."""
        wide = CSS[CSS.index("@media (min-width: 1251px)"):]
        wide = wide[:wide.index("@media (max-width: 1250px)")]
        assert ":has(> .st-key-freshness)" in wide and "margin-bottom: -1rem" in wide
        narrow = CSS[CSS.index("@media (max-width: 1250px)"):]
        narrow = narrow[:narrow.index("\n}\n")]
        assert "margin-bottom: -1rem" not in narrow

    def test_the_bar_is_level_with_the_tab_labels_after_the_tabs_moved_up(self):
        rule = CSS[CSS.index(".st-key-freshness {"):]
        assert "top: 10px !important" in rule[:rule.index("}")]


class TestThePageStart:
    def test_it_starts_below_streamlits_header_band(self):
        """The Cloud toolbar sits in that band; content under it overlaps at a
        narrow width."""
        rule = CSS[CSS.index(".block-container {"):]
        assert "padding-top: 3.75rem" in rule[:rule.index("}")]


class TestRadioRows:
    def test_a_row_is_as_tall_as_its_options_and_the_options_carry_the_room(self):
        group = CSS[CSS.index('[data-testid="stRadioGroup"] {'):]
        group = group[:group.index("}")]
        assert "min-height: 0" in group
        assert re.search(r'\[data-testid="stRadioOption"\] \{ padding: 4px 2px 4px 0; \}', CSS)
