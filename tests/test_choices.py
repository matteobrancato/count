"""Every pick-one control on the page is the same radio.

Coverage had radio buttons while the scope, the run, the granularity and the
release kind were button-shaped segmented controls — two looks for one gesture.
They all go through `choice.pick` now.  What can go wrong is quiet: a control
that drifts back to another widget, a label drawn with its icon that comes back
to the caller as text (every run is keyed by its plain name), the same
population named differently on two tabs, a seeded choice that a widget resets.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest
from streamlit.proto.LabelVisibility_pb2 import LabelVisibility
from streamlit.testing.v1 import AppTest

from src.ui import backlog_tab as bl
from src.ui import coverage_tab, leakage_tab

ROOT = pathlib.Path(__file__).resolve().parent.parent
COLLAPSED = LabelVisibility.LabelVisibilityOptions.COLLAPSED


def _sources():
    yield ROOT / "app.py"
    yield from sorted((ROOT / "src").rglob("*.py"))


# ── one widget for one gesture ───────────────────────────────────────────────
class TestOneControl:
    def test_no_segmented_control_or_pills_is_left(self):
        found = [f"{p.name}:{n}" for p in _sources()
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
                 if re.search(r"\.(segmented_control|pills)\(", line)]
        assert found == []

    def test_a_radio_is_only_ever_drawn_by_the_helper(self):
        """Elsewhere it would be a second look for the same job, one styling
        rule away from a new mismatch."""
        found = [p.name for p in _sources()
                 if p.name != "choice.py" and "st.radio(" in p.read_text(encoding="utf-8")]
        assert found == []

    def test_the_four_controls_that_were_buttons_use_it(self):
        from src.ui import global_filter
        for fn in (global_filter.render, bl._run_picker, coverage_tab._view_controls,
                   leakage_tab._pick_release):
            assert "choice.pick(" in inspect.getsource(fn), fn.__qualname__

    def test_the_button_group_css_is_gone_and_the_radio_has_its_rule(self):
        css = (ROOT / "src" / "ui" / "styles.py").read_text(encoding="utf-8")
        assert "stButtonGroup" not in css
        assert 'data-testid="stRadioOption"' in css


class TestThePick:
    @staticmethod
    def _app():
        import streamlit as st

        from src.ui import choice
        got = choice.pick("Pick", ["a", "b", "c"], key="k", format_func=str.upper)
        st.write(f"got={got}")

    def test_it_shows_the_label_and_returns_the_option(self):
        at = AppTest.from_function(self._app).run()
        assert at.radio[0].options == ["A", "B", "C"]
        assert at.markdown[0].value == "got=a"
        at.radio[0].set_value("c").run()
        assert at.markdown[0].value == "got=c"
        assert at.session_state["k"] == "c"

    def test_it_is_horizontal_and_hides_its_label(self):
        at = AppTest.from_function(self._app).run()
        proto = at.radio[0].proto
        assert proto.horizontal is True
        assert proto.label_visibility.value == COLLAPSED

    def test_a_seeded_choice_is_kept_without_a_warning(self):
        def app():
            import streamlit as st

            from src.ui import choice
            st.session_state.setdefault("k", "b")
            choice.pick("Pick", ["a", "b", "c"], key="k")
        at = AppTest.from_function(app).run()
        assert at.radio[0].value == "b"
        assert not at.warning and not at.exception

    def test_a_given_index_is_the_start(self):
        def app():
            from src.ui import choice
            choice.pick("Pick", ["a", "b", "c"], key="k", index=2)
        assert AppTest.from_function(app).run().radio[0].value == "c"


# ── each control, as the page draws it ───────────────────────────────────────
class TestScope:
    def test_the_scope_is_a_radio_that_cannot_be_cleared(self):
        def app():
            from src.ui import global_filter
            global_filter.render()
        at = AppTest.from_function(app, default_timeout=30).run()
        from src.ui import global_filter as gf
        assert at.radio[0].options == [gf._SCOPE_LABELS[s] for s in gf.scopes_available()]
        assert at.radio[0].value == at.radio[0].options[0]
        assert not at.exception


class TestRun:
    @staticmethod
    def _app():
        from src.ui import backlog_tab
        backlog_tab._run_picker("website")

    def test_every_run_has_an_icon(self):
        assert set(bl._RUN_ICONS) == set(bl.RUNS)

    def test_the_label_has_the_icon_and_the_value_is_the_run(self):
        at = AppTest.from_function(self._app).run()
        assert at.radio[0].options == [f"{bl._RUN_ICONS[r]} {r}" for r in bl.RUNS]
        assert at.radio[0].value == bl.RUN_BIG
        at.radio[0].set_value(bl.RUN_SMOKE).run()
        assert at.session_state["bl_run_website"] == bl.RUN_SMOKE   # not "🔥 Smoke"

    def test_the_sentence_under_it_follows_the_run(self):
        at = AppTest.from_function(self._app).run()
        assert at.caption[0].value == bl._RUN_MEANING[bl.RUN_BIG]
        at.radio[0].set_value(bl.RUN_PS).run()
        assert at.caption[0].value == bl._RUN_MEANING[bl.RUN_PS]

    def test_the_window_has_a_picker_of_its_own(self):
        def app():
            from src.ui import backlog_tab
            backlog_tab._run_picker("website")
            backlog_tab._run_picker("website", popup=True)
        at = AppTest.from_function(app).run()
        assert len(at.radio) == 2 and not at.exception


class TestCoverage:
    @staticmethod
    def _app(scope):
        import streamlit as st

        from src.ui import coverage_tab
        view, depth = coverage_tab._view_controls(scope, "Drogas")
        st.write(f"{view}|{depth}|{type(depth).__name__}")

    def test_a_view_and_a_depth(self):
        at = AppTest.from_function(self._app, args=("website",)).run()
        assert at.radio[0].options == coverage_tab._VIEW_OPTIONS
        assert at.radio[1].options == list(coverage_tab._GRAN_LABELS.values())
        assert at.markdown[-1].value.endswith("|0|int")
        at.radio[1].set_value(2).run()
        assert at.markdown[-1].value.endswith("|2|int")

    def test_mobile_app_has_its_own_baseline_label(self):
        at = AppTest.from_function(self._app, args=("mobile_app",)).run()
        assert at.radio[0].options == coverage_tab._VIEW_OPTIONS_MAPP

    def test_each_scope_and_bu_keeps_its_own_choice(self):
        at = AppTest.from_function(self._app, args=("website",)).run()
        keys = {k for k in at.session_state.filtered_state}
        assert {"cov_view_website_Drogas", "cov_gran_website_Drogas"} <= keys

    def test_the_depths_are_named_so_nobody_decodes_plus_two(self):
        labels = list(coverage_tab._GRAN_LABELS.values())
        assert labels == ["Main", "Sub", "Sub 2", "Sub 3"]
        assert all(label in coverage_tab._GRAN_HELP for label in ("Main", "Sub", "Sub 2", "Sub 3"))

    def test_the_depth_has_its_own_line_under_the_views(self):
        """Five views with their icons fill the window; beside them the depth
        wrapped under them at some widths and not others."""
        src = inspect.getsource(coverage_tab._view_controls)
        assert 'with st.container(gap="xsmall"):' in src


class TestOneNamePerPopulation:
    """The Backlog's run and Coverage's view are the same set of cases; a reader
    who moves from one tab to the other should not have to wonder."""

    @pytest.mark.parametrize("run, view", [
        (bl.RUN_BIG, coverage_tab._VIEW_REGR),
        (bl.RUN_SMOKE, coverage_tab._VIEW_SMOKE),
        (bl.RUN_PS, coverage_tab._VIEW_PS),
        (bl.RUN_EXT, coverage_tab._VIEW_EXT),
    ])
    def test_same_icon_same_name(self, run, view):
        assert view == f"{bl._RUN_ICONS[run]} {run}"


class TestReleaseKind:
    def test_released_or_not_is_a_radio_beside_the_release_list(self):
        def app():
            import datetime as dt
            from types import SimpleNamespace

            from src.ui import leakage_tab
            rels = [SimpleNamespace(name="R1", released=True, date=dt.date(2026, 10, 1)),
                    SimpleNamespace(name="R2", released=False, date=dt.date(2026, 12, 1))]
            leakage_tab._pick_release(SimpleNamespace(project="P"), rels)
        at = AppTest.from_function(app, default_timeout=30).run()
        assert at.radio[0].options == list(leakage_tab._KINDS)
        assert at.selectbox[0].options[0].startswith("R1")
        at.radio[0].set_value(leakage_tab._KINDS[1]).run()
        assert at.selectbox[0].options[0].startswith("R2")
        assert not at.exception


class TestTheLook:
    def test_the_chosen_option_differs_by_colour_not_weight(self):
        """A bolder label is wider, so a click would shift the options beside it."""
        css = (ROOT / "src" / "ui" / "styles.py").read_text(encoding="utf-8")
        rule = css[css.index("Pick-one controls"):css.index("Granularity — a quiet")]
        assert 'data-selected="true"' in rule
        assert "font-weight" not in rule
        assert "white-space: nowrap" in rule
