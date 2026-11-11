"""A scope shows its own numbers, or says it has none.

Found on the live app on 2026-11-10, none of them caught by a test:
- Microservices on Production Sanity listed the WEBSITE BUs, under the
  Microservices heading: the table fell back to every scope's rows "so it is
  never blank";
- Mobile App on Production Sanity / Extended did the same, its sanity runs
  never having been built;
- the Coverage tab's Mobile App sanity views showed the whole baseline under a
  sanity name (Marionnaud: 570 rows, 15.8%, on all three);
- the sentence under the run picker told a Mobile App reader the baseline is
  the big_regr labels, which it is not;
- the Overview counted Marionnaud's Mobile App automation as 0 where the
  Backlog counted 90: the mobile rules kept a Regression-only type filter the
  baseline does not have.
"""
from __future__ import annotations

import pandas as pd
from streamlit.testing.v1 import AppTest

from src import bu_rules as br
from src.ui import backlog_tab as bl
from src.ui import coverage_tab


def _render(scope: str, rows_scope: str):
    """The Backlog table drawn under *scope* from a summary whose only row
    belongs to *rows_scope*."""
    def app(scope, rows_scope):
        import pandas as pd

        from src.ui import backlog_tab as bl
        row = dict.fromkeys(bl._SUMMARY_COLUMNS, 0)
        row.update({"BU": "Drogas", "Scope": rows_scope, "Total": 10, "Automated": 5,
                    "Coverage %": 50.0, "Coverage excl. Partially %": 50.0})
        summary = pd.DataFrame([row])
        saved = (bl._run_data, bl.global_filter.current)
        bl._run_data = lambda run, sc: (summary, {}, {})
        bl.global_filter.current = lambda: (scope, "Drogas")
        try:
            bl.render()
        finally:
            bl._run_data, bl.global_filter.current = saved
    return AppTest.from_function(app, args=(scope, rows_scope)).run()


class TestAScopeNeverShowsAnothersRows:
    def test_an_empty_scope_says_so_instead_of_borrowing_rows(self):
        at = _render("next_gen", "Website")
        assert not at.exception, at.exception
        drawn = " ".join(m.value for m in at.markdown)
        assert "Drogas" not in drawn, "a Website BU was drawn under Microservices"
        assert any("No Big No-Regression rows for" in i.value for i in at.info)

    def test_a_scope_with_rows_still_draws_them(self):
        """The control: the same harness does see the row when it belongs."""
        at = _render("next_gen", "Microservices")
        assert not at.exception, at.exception
        assert "Drogas" in " ".join(m.value for m in at.markdown)


class TestTheMobileAppOffersOnlyWhatItDefines:
    def test_no_sanity_run_on_the_mobile_app(self):
        assert bl.runs_for("mobile_app") == [bl.RUN_BIG, bl.RUN_SMALL, bl.RUN_SMOKE]
        assert bl.runs_for("website") == bl.RUNS
        assert bl.runs_for("next_gen") == bl.RUNS

    def test_no_sanity_view_on_the_mobile_app(self):
        sanity = set(coverage_tab._SANITY_VIEWS)
        assert not sanity & set(coverage_tab._VIEW_OPTIONS_MAPP)
        assert sanity <= set(coverage_tab._VIEW_OPTIONS)

    def test_backlog_and_coverage_offer_the_same_populations(self):
        """A run on one tab and a view on the other are one population."""
        names = {v.split(" ", 1)[1] for v in coverage_tab._VIEW_OPTIONS_MAPP}
        assert names - {"Baseline"} == {r for r in bl.runs_for("mobile_app")
                                        if r != bl.RUN_BIG} - {bl.RUN_SMALL}

    def test_the_expansion_refuses_a_sanity_label_on_the_mobile_app(self):
        """Asked for a sanity population it has no notion of, the MAPP
        expansion returned the whole baseline under the sanity's name."""
        rules = [r for r in br.ALL_RULES if r.scope == "mobile_app" and r.bu == "Marionnaud"]
        nd = pd.DataFrame([{"case_id": 1, "priority_label": "High", "deprecated": False,
                            "section_path": "A", "labels": ["prod_sanity"],
                            "status_Automation Status": "Automated"}])
        _nd, _auto, ids, expanded = coverage_tab._baseline_like_backlog(
            nd, pd.DataFrame(columns=["case_id", "country_label", "device"]),
            rules, member_label=br.PROD_SANITY_LABEL)
        assert expanded.empty and not ids
        # ...while the baseline itself is still built.
        _nd, _auto, _ids, base = coverage_tab._baseline_like_backlog(
            nd, pd.DataFrame(columns=["case_id", "country_label", "device"]), rules)
        assert not base.empty

    def test_a_withdrawn_run_left_in_the_session_falls_back_to_the_baseline(self):
        def app():
            import streamlit as st

            from src.ui import backlog_tab as bl
            st.session_state.setdefault("bl_run_mobile_app", bl.RUN_PS)
            bl._run_picker("mobile_app")
        at = AppTest.from_function(app).run()
        assert not at.exception, at.exception
        assert at.radio[0].value == bl.RUN_BIG

    def test_a_withdrawn_view_left_in_the_session_falls_back_to_the_baseline(self):
        def app():
            import streamlit as st

            from src.ui import coverage_tab
            st.session_state.setdefault("cov_view_mobile_app_Drogas", coverage_tab._VIEW_PS)
            coverage_tab._view_controls("mobile_app", "Drogas")
        at = AppTest.from_function(app).run()
        assert not at.exception, at.exception
        assert at.radio[0].value == coverage_tab._VIEW_REGR_MAPP


class TestTheRunIsDescribedInItsScopesTerms:
    def test_the_mobile_app_baseline_is_not_called_big_regr(self):
        text = bl._run_meaning(bl.RUN_BIG, "mobile_app")
        assert "big_regr" not in text and "High" in text and "iOS" in text

    def test_the_website_sentence_is_unchanged(self):
        for run in bl.RUNS:
            assert bl._run_meaning(run, "website") == bl._RUN_MEANING[run]


class TestTheMobileRulesReadEveryType:
    def test_no_type_filter(self):
        """The baseline admits a case by priority whatever its Type, so the
        automated set the Overview reads must too."""
        for r in br.ALL_RULES:
            if r.scope == "mobile_app":
                assert r.type_filter == [], r.name
