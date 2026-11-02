"""The Business Unit window, and the page around it.

A click on a BU in the Backlog table opens a window with its Detail, Coverage,
Leakage and Trend, where there used to be a dropdown and three tabs.  What can
go wrong is quiet: a row that does not carry its BU, a table drawn one click
behind the window, a BU name that breaks the markup, a tab that is lost in the
move.  These tests hold those, and the previous layout kept beside the new one.
"""
from __future__ import annotations

import contextlib
import inspect
import types

import pandas as pd
import pytest
import streamlit as st

from src import freshness, martech
from src.ui import backlog_tab as bl
from src.ui import bu_popup, classic_view, isolation, martech_tab
from src.ui import global_filter as gf

NUM = ["Total", "Automated", "Backlog"]


def _display(*bus):
    return pd.DataFrame([{"BU": bu, "Scope": "Website", "Total": 10, "Automated": 5,
                          "Backlog": 2, "Coverage %": 50.0} for bu in bus])


# ── the markup a click is read from ──────────────────────────────────────────
class TestClickableRows:
    def test_a_clickable_table_says_so_and_every_row_carries_its_bu(self):
        out = bl._summary_table_html(_display("Kruidvat", "Drogas"), NUM, clickable=True)
        assert 'class="bl-summary bl-clickable"' in out
        assert out.count('<tr data-bu="Kruidvat">') == 1
        assert out.count('<tr data-bu="Drogas">') == 1
        assert out.count('class="bu-link"') == 2

    def test_the_name_is_a_real_button_so_the_keyboard_reaches_it(self):
        out = bl._summary_table_html(_display("Kruidvat"), NUM, clickable=True)
        assert '<button type="button" class="bu-link"' in out
        assert 'aria-haspopup="dialog"' in out
        assert '<span class="bu-name">Kruidvat</span><span class="bu-go">›</span>' in out

    def test_without_it_the_markup_is_what_it_always_was(self):
        out = bl._summary_table_html(_display("Kruidvat", "Drogas"), NUM)
        assert "data-bu" not in out and "bl-clickable" not in out and "<button" not in out
        assert '<td class="l bu">Kruidvat</td>' in out
        assert out.startswith('<div class="bl-summary"><table>')

    def test_the_opened_bu_keeps_its_mark_in_both(self):
        for clickable in (False, True):
            out = bl._summary_table_html(_display("Kruidvat", "Drogas"), NUM,
                                         selected_bu="Drogas", clickable=clickable)
            marked = [r for r in out.split("<tbody>")[1].split("<tr") if "class='sel'" in r]
            assert len(marked) == 1 and "Drogas" in marked[0] and "Kruidvat" not in marked[0]

    @pytest.mark.parametrize("name", ['Say "hi"', "A & B", "<script>x</script>", "O'Neil"])
    def test_a_bu_name_cannot_break_out_of_its_markup(self, name):
        out = bl._summary_table_html(_display(name), NUM, clickable=True)
        assert "<script>" not in out
        # the attribute is quoted and escaped, so it reads back as the name
        import html
        import re
        attrs = re.findall(r'<tr data-bu="([^"]*)"', out)
        assert [html.unescape(a) for a in attrs] == [name]

    def test_the_helpers_agree_with_the_table(self):
        assert bu_popup.row_attr("Kruidvat", True) == ' data-bu="Kruidvat"'
        assert bu_popup.row_attr("Kruidvat", False) == ""
        assert bu_popup.name_cell("Kruidvat", False, "2 runs") == (
            '<td class="l bu">Kruidvat<span class="sub">2 runs</span></td>')

    def test_the_click_handler_reads_what_the_table_writes(self):
        """The strings are one contract, in two languages."""
        js = bu_popup._CLICK_JS
        assert ".bl-summary.bl-clickable tr[data-bu]" in js
        assert "row.dataset.bu" in js
        assert "('bu', row.dataset.bu)" in js

    def test_one_handler_for_the_page_calls_the_latest_trigger(self):
        """A handler bound at each mount kept calling the `setTriggerValue` it was
        born with: after the whole page reran (a change of scope) every click went
        to a fragment that no longer existed and was dropped.  Live, 2026-11-02:
        the browser said "Multiple different fragmentIds"."""
        js = bu_popup._CLICK_JS
        assert js.count("addEventListener") == 1
        assert "if (!window.__countBuClick)" in js          # attached once, never again
        assert "window.__countBuTrigger = setTriggerValue" in js   # every run refreshes it
        assert "window.__countBuTrigger('bu'" in js                # and the handler reads it live
        assert "removeEventListener" not in js


# ── the window ───────────────────────────────────────────────────────────────
class TestWindow:
    def test_it_opens_large_and_named_after_the_bu(self, monkeypatch):
        seen = {}

        def fake_dialog(title, **kw):
            seen.update(title=title, **kw)
            return lambda fn: (lambda *a: seen.update(fn=fn, args=a))
        monkeypatch.setattr(bu_popup.st, "dialog", fake_dialog)
        bu_popup.open_for("website", "Kruidvat")
        assert (seen["title"], seen["width"], seen["args"]) == ("Kruidvat", "large",
                                                                ("website", "Kruidvat"))
        bu_popup.open_for("mobile_app", "Kruidvat")
        assert seen["title"] == f"Kruidvat · {gf.scope_label('mobile_app')}"

    def test_it_holds_what_the_old_tabs_held(self):
        labels = [label for label, _fn, _anim in bu_popup._sections()]
        assert labels == ["🔎 Detail", "📐 Coverage", "🐞 Leakage", "📈 Trend"]

    def test_opening_it_starts_its_run_picker_on_the_tables_run(self, monkeypatch):
        st.session_state.clear()
        opened = []
        monkeypatch.setattr(bl.bu_popup, "open_for", lambda scope, bu: opened.append((scope, bu)))
        bl._open_bu("website", "Drogas", "Smoke")
        assert st.session_state["bl_run_website_bu"] == "Smoke"
        assert opened == [("website", "Drogas")]
        st.session_state.clear()

    def test_the_run_pickers_never_share_a_key_in_one_run(self):
        """The window is drawn in the run that opens it, beside the table's own
        picker: one key twice is a DuplicateWidgetId."""
        src = inspect.getsource(bl._run_picker)
        assert 'f"bl_run_{scope}" + ("_bu" if popup else "")' in src

    def test_the_detail_in_the_window_uses_its_own_picker(self):
        assert inspect.signature(bl.render_detail).parameters["popup"].default is False
        assert "popup=True" in inspect.getsource(bu_popup._sections)


class TestTableAndWindowAgree:
    def test_the_click_is_read_at_page_level_before_the_table_is_drawn(self):
        """Read after, the table shows the BU opened BEFORE this one while the
        window shows the new one — seen once, in the first harness."""
        order = [inspect.getsource(bl.render_page).index(part) for part in
                 ("bu_popup.clicked()", "select_bu(", "hand_over(", "render()")]
        assert order == sorted(order)

    def test_the_window_opens_after_the_table_in_both_scopes(self):
        for fn, table, open_window in ((bl.render, "_summary_table_html(", "_open_bu("),
                                       (martech_tab.render, "_table_html(", "open_for(")):
            src = inspect.getsource(fn)
            assert src.index(table) < src.index(open_window), fn.__module__

    def test_the_listener_is_not_inside_a_fragment(self):
        """A trigger inside a fragment reruns that fragment, by an id that went
        stale on a page rerun.  The page-level function is no fragment; the table's is."""
        assert "clicked()" not in inspect.getsource(bl.render)
        assert "clicked()" not in inspect.getsource(martech_tab.render)
        assert not hasattr(bl.render_page, "__wrapped__") or "fragment" not in repr(bl.render_page)

    def test_a_handed_over_bu_is_taken_once(self):
        st.session_state.clear()
        assert bu_popup.take_handed_over() is None
        bu_popup.hand_over("Drogas")
        assert bu_popup.take_handed_over() == "Drogas"
        assert bu_popup.take_handed_over() is None        # never opens a window twice
        st.session_state.clear()

    def test_the_fragment_takes_the_hand_over_before_anything_can_fail(self):
        src = inspect.getsource(bl.render)
        assert src.index("take_handed_over()") < src.index("global_filter.current()")

    def test_the_page_draws_the_backlog_through_render_page(self):
        import app
        fn = app._SECTIONS[0][1]
        assert (fn.__module__, fn.__name__) == ("src.ui.backlog_tab", "render_page")

    def test_the_inert_table_takes_its_bu_from_the_dropdown(self):
        src = inspect.getsource(bl.render)
        assert "global_filter.opened(scope) if pick_bu else bu" in src
        assert inspect.signature(bl.render).parameters["pick_bu"].default is True

    def test_martech_is_drawn_by_its_own_module(self):
        assert "martech_tab.render(picked=picked, pick_bu=pick_bu)" in inspect.getsource(bl.render)


# ── the page ─────────────────────────────────────────────────────────────────
class TestPage:
    def test_the_tabs_are_the_table_the_cross_bu_view_and_the_old_layout(self):
        import app
        labels = [label for label, _fn, _anim in app._SECTIONS]
        assert labels[0] == "📋 Backlog"
        assert labels[-1].startswith("🕰️")
        assert any("All BUs" in label for label in labels)
        for gone in ("Detail", "Coverage", "Trend"):
            assert not any(gone in label for label in labels), gone    # in the window now

    def test_the_old_layout_keeps_all_six_of_its_tabs(self):
        assert [label for label, _fn, _anim in classic_view._SECTIONS] == [
            "📋 Backlog", "🔎 Detail", "📐 Coverage", "🐞 Leakage", "📈 Trend", "🌍 All BUs"]
        assert all(callable(fn) for _l, fn, _a in classic_view._SECTIONS)

    def test_the_old_layout_follows_its_dropdown(self, monkeypatch):
        st.session_state.clear()
        monkeypatch.setattr(st, "query_params", {}, raising=False)
        st.session_state["classic_bu_website"] = "Drogas"
        classic_view._follow("website", "classic_bu_website")
        assert gf.opened("website") == "Drogas"
        assert gf.current() == ("website", "Drogas")
        st.session_state.clear()

    def test_the_old_layout_draws_the_inert_table(self):
        assert "backlog_tab.render(pick_bu=False)" in inspect.getsource(classic_view)

    def test_there_is_no_scope_specific_dropdown_any_more(self):
        assert "selectbox" not in inspect.getsource(gf.render)


class TestIsolation:
    def _fake_st(self, monkeypatch):
        calls = []
        fake = types.SimpleNamespace(
            container=lambda key=None: contextlib.nullcontext(),
            error=lambda msg: calls.append(("error", msg)),
            expander=lambda label: contextlib.nullcontext(),
            code=lambda text: calls.append(("code", text)))
        monkeypatch.setattr(isolation, "st", fake)
        return calls

    def test_a_failing_section_becomes_an_error_in_its_own_place(self, monkeypatch):
        calls = self._fake_st(monkeypatch)

        def boom():
            raise ValueError("no data")
        isolation.render_isolated(boom, "Coverage · Drogas", "coverage_anim")
        assert calls[0][0] == "error" and "Coverage · Drogas" in calls[0][1] and "no data" in calls[0][1]

    def test_a_working_section_draws_normally(self, monkeypatch):
        calls = self._fake_st(monkeypatch)
        drawn = []
        isolation.render_isolated(lambda: drawn.append(1), "Detail")
        assert drawn == [1] and calls == []

    def test_streamlits_own_control_flow_is_not_swallowed(self, monkeypatch):
        """st.rerun and st.stop are BaseExceptions: catching them would freeze a
        rerun that was asked for."""
        self._fake_st(monkeypatch)

        class Rerun(BaseException):
            pass

        def rerun():
            raise Rerun()
        with pytest.raises(Rerun):
            isolation.render_isolated(rerun, "Leakage")


def test_relative_time_reads_like_the_freshness_bar(monkeypatch):
    monkeypatch.setattr(freshness.time, "time", lambda: 100_000.0)
    assert freshness.relative_time(100_000.0 - 10) == "just now"
    assert freshness.relative_time(100_000.0 - 5 * 60) == "5m ago"
    assert freshness.relative_time(100_000.0 - 3 * 3600) == "3h ago"
    assert freshness.relative_time(100_000.0 - 2 * 86400) == "2d ago"
    assert freshness.relative_time(100_000.0 + 50) == "just now"      # a clock ahead of ours


# ── Martech's page ───────────────────────────────────────────────────────────
STATUSES = {1: "Passed", 2: "Blocked", 3: "To be tested", 6: "On hold", 7: "N/A"}


def _runs(*specs):
    return [martech.Run(i, name, martech.bu_of_run(name), counts)
            for i, (name, counts) in enumerate(specs, 1)]


class TestMartechTable:
    def _table(self, clickable=True, selected=""):
        runs = _runs(("KV NL", {1: 2, 2: 5, 7: 1}), ("KV BE", {2: 4}), ("MFR", {6: 7}),
                     ("SD", {7: 3}))
        summary = martech.summary(runs, STATUSES, order=["Kruidvat", "Marionnaud"])
        return martech_tab._table_html(summary, selected, clickable)

    def test_every_bu_is_a_row_that_opens_its_window(self):
        out = self._table()
        assert 'class="bl-summary bl-clickable"' in out
        for bu in ("Kruidvat", "Marionnaud", "Superdrug"):
            assert f'<tr data-bu="{bu}">' in out

    def test_the_headers_are_the_figures_and_the_statuses_that_occur(self):
        head = self._table().split("<tbody>")[0]
        for text in ("Business Unit", "Total", "Merged", "Backlog", "Blocked", "On hold",
                     "Not Applicable", "Progress"):
            assert text in head
        assert "To be tested" not in head                 # no run has one

    def test_header_and_body_cell_counts_agree(self):
        out = self._table()
        head, body = out.split("<tbody>")
        n_head = head.count("<th")
        for row in body.split("</tr>")[:-1]:
            assert row.count("<td") == n_head - head.count("<thead")

    def test_the_run_count_is_said_under_the_name(self):
        out = self._table()
        assert 'Kruidvat</span><span class="bu-go">›</span></button><span class="sub">2 runs</span>' in out
        assert '<span class="sub">1 run</span>' in out

    def test_progress_is_never_coloured_against_the_coverage_target(self):
        out = self._table()
        assert "#16A34A" not in out and "#DC2626" not in out and "#F59E0B" not in out

    def test_a_bu_with_nothing_but_na_shows_no_bar(self):
        out = self._table()
        sd_row = out.split('<tr data-bu="Superdrug">')[1].split("</tr>")[0]
        assert "cov-track" not in sd_row and "—" in sd_row

    def test_inert_it_has_no_click_markup(self):
        out = self._table(clickable=False)
        assert "data-bu" not in out and "bl-clickable" not in out and "<button" not in out

    def test_the_opened_bu_is_marked(self):
        assert self._table(selected="Marionnaud").count("class='sel'") == 1

    def test_a_bu_name_from_the_plan_is_escaped(self):
        runs = _runs(("<b>New</b> 1", {2: 3}))
        out = martech_tab._table_html(martech.summary(runs, STATUSES), "", True)
        assert "<b>New</b>" not in out and "&lt;b&gt;New&lt;/b&gt;" in out


class TestMartechScope:
    def setup_method(self):
        st.session_state.clear()

    def teardown_method(self):
        st.session_state.clear()

    def test_it_is_offered_only_once_its_key_is_in_the_secrets(self, monkeypatch):
        monkeypatch.setattr(gf.martech, "configured", lambda: False)
        assert "martech" not in gf.scopes_available()
        monkeypatch.setattr(gf.martech, "configured", lambda: True)
        assert gf.scopes_available()[-1] == "martech"
        assert gf.scopes_available()[:3] == ["website", "mobile_app", "next_gen"]

    def test_its_bus_come_in_the_dashboards_order(self):
        bus = gf.bus_for_scope("martech")
        assert bus == [b for b in gf._BU_ORDER if b in martech.KNOWN_BUS]

    def test_a_bu_from_the_plan_is_kept_even_if_no_list_knows_it(self, monkeypatch):
        monkeypatch.setattr(gf.martech, "configured", lambda: True)
        st.session_state["global_scope"] = gf.scope_label("martech")
        st.session_state["global_bu_martech"] = "Brand New 1"
        assert gf.current() == ("martech", "Brand New 1")

    def test_nothing_opened_is_an_empty_bu_not_a_guess(self, monkeypatch):
        monkeypatch.setattr(gf.martech, "configured", lambda: True)
        st.session_state["global_scope"] = gf.scope_label("martech")
        assert gf.current() == ("martech", "") and gf.opened("martech") == ""

    def test_the_overview_and_the_hygiene_checks_say_they_do_not_apply(self):
        from src.ui import data_quality
        assert data_quality._has_baseline("website") is True
        assert data_quality._has_baseline("martech") is False
        assert data_quality.finding_count("martech") is None


class TestMartechView:
    def test_a_failure_to_read_is_an_error_not_a_zero(self, monkeypatch):
        errors = []
        monkeypatch.setattr(martech_tab.martech, "fetch_plan",
                            lambda: (_ for _ in ()).throw(martech.MartechError("HTTP 401")))
        monkeypatch.setattr(martech_tab.st, "spinner", lambda *_: contextlib.nullcontext())
        monkeypatch.setattr(martech_tab.st, "error", errors.append)
        assert martech_tab._load() is None
        assert errors and "HTTP 401" in errors[0]

    def test_a_plan_without_runs_says_so(self, monkeypatch):
        infos = []
        monkeypatch.setattr(martech_tab.martech, "fetch_plan",
                            lambda: {"plan": {"entries": []}, "read_at": 0, "base_url": "x"})
        monkeypatch.setattr(martech_tab.martech, "fetch_statuses", lambda: STATUSES)
        monkeypatch.setattr(martech_tab.st, "spinner", lambda *_: contextlib.nullcontext())
        monkeypatch.setattr(martech_tab.st, "info", infos.append)
        assert martech_tab._load() is None and infos

    def test_the_excel_is_built_when_it_is_asked_for(self):
        built = []
        frame = pd.DataFrame({"Case": ["u"], "Title": ["t"], "Run": ["KV NL"], "Status": ["Blocked"]})
        writer = martech_tab._xlsx(frame, "Kruidvat")
        assert built == []
        payload = writer()
        assert payload[:2] == b"PK"                        # an xlsx is a zip
        out = pd.read_excel(__import__("io").BytesIO(payload))
        assert list(out["Status"]) == ["Blocked"]

    def test_the_legend_states_what_the_plan_states(self):
        for text in ("Passed", "merged", "Accepted fail", "PR raised", "Failed",
                     "MQA", "N/A", "not applicable"):
            assert text in martech_tab._LEGEND
