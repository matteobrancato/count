"""Regression tests for parsing / integration helpers.

Complements `test_business_rules.py` (which covers the counting rules): here we
lock down the input parsing, the scope+BU selector state machine, the Jira
client's graceful degradation, and the Report's regression-flag join.
"""
from __future__ import annotations

import pandas as pd
import pytest
import streamlit as st

from src import jira_client as jc
from src.rules_engine import _mapp_devices_for
from src.ui import global_filter as gf
from src.ui import report_tab as rt


# ── MAPP operating-system field → device rows ────────────────────────────────
class _Meta:
    system_name = "custom_os"
    values_by_id = {1: "iOS", 2: "Android", 3: "Both"}


class _Reg:
    def field(self, label):
        return _Meta() if label == "MAPP Automation Operating System" else None


class _RegMissing:
    def field(self, label):
        return None


class TestMappDevices:
    @pytest.mark.parametrize("raw,expected", [
        (1, ["iOS"]),
        (2, ["Android"]),
        (3, ["iOS", "Android"]),          # "Both" → two rows
        (None, ["Unspecified"]),
    ])
    def test_os_field_maps_to_devices(self, raw, expected):
        assert _mapp_devices_for({"custom_os": raw}, _Reg()) == expected

    def test_missing_field_degrades_gracefully(self):
        assert _mapp_devices_for({}, _RegMissing()) == ["Unspecified"]

    def test_matching_is_case_insensitive(self):
        class Lower(_Meta):
            values_by_id = {1: "ios", 2: "ANDROID", 3: "both"}

        class R:
            def field(self, label):
                return Lower() if "MAPP" in label else None

        assert _mapp_devices_for({"custom_os": 1}, R()) == ["iOS"]
        assert _mapp_devices_for({"custom_os": 3}, R()) == ["iOS", "Android"]


# ── case-id parsing (In-depth Test Analysis input) ───────────────────────────
# ── JIRA key extraction from TestRail defect fields ──────────────────────────
# ── BU matching from run/plan names ──────────────────────────────────────────
# ── global scope + BU selector state machine ─────────────────────────────────
class TestGlobalFilter:
    def setup_method(self):
        st.session_state.clear()

    def teardown_method(self):
        st.session_state.clear()

    def test_defaults_to_first_scope_and_bu(self):
        scope, bu = gf.current()
        assert scope == "website"
        assert bu == gf.bus_for_scope("website")[0]

    def test_remembers_bu_per_scope(self):
        st.session_state["global_scope"] = gf.scope_label("website")
        bus = gf.bus_for_scope("website")
        st.session_state["global_bu_website"] = bus[-1]
        assert gf.current() == ("website", bus[-1])

    def test_invalid_bu_falls_back_instead_of_crashing(self):
        """A BU stored for one scope must never leak into another."""
        st.session_state["global_scope"] = gf.scope_label("website")
        st.session_state["global_bu_website"] = "NOT_A_REAL_BU"
        _scope, bu = gf.current()
        assert bu in gf.bus_for_scope("website")

    def test_every_scope_exposes_only_its_own_bus(self):
        for scope in gf.scopes_available():
            for bu in gf.bus_for_scope(scope):
                assert bu, "empty BU name"


class TestShareableLinks:
    """?scope=&bu= makes a view linkable — the basis of "look at this" at work."""

    def setup_method(self):
        st.session_state.clear()
        st.query_params = dict()

    def teardown_method(self):
        st.session_state.clear()

    def test_url_selects_scope(self):
        st.query_params = {"scope": "next_gen"}
        gf._seed_from_url()
        assert gf.current()[0] == "next_gen"

    def test_url_selects_scope_and_bu(self):
        bus = gf.bus_for_scope("website")
        st.query_params = {"scope": "website", "bu": bus[-1]}
        gf._seed_from_url()
        assert gf.current() == ("website", bus[-1])

    def test_unknown_values_fall_back_without_error(self):
        st.query_params = {"scope": "does-not-exist", "bu": "NOT_A_BU"}
        gf._seed_from_url()
        scope, bu = gf.current()
        assert scope == "website" and bu in gf.bus_for_scope("website")

    def test_seeding_happens_once_and_never_fights_the_user(self):
        st.query_params = {"scope": "next_gen"}
        gf._seed_from_url()
        st.session_state["global_scope"] = gf.scope_label("website")  # user clicks
        gf._seed_from_url()                                           # next rerun
        assert gf.current()[0] == "website"

    def test_selection_is_published_back_to_the_url(self):
        gf._publish_to_url("website", "Drogas")
        assert st.query_params["scope"] == "website"
        assert st.query_params["bu"] == "Drogas"


# ── Jira client: read-only, degrades silently when unconfigured ──────────────
class TestJiraClient:
    def test_url_normalisation_strips_jira_suffix(self, monkeypatch):
        monkeypatch.setattr(jc.st, "secrets", {
            "JIRA_URL": "https://x.atlassian.net/jira/",
            "ATLASSIAN_USER": "u", "ATLASSIAN_API_KEY": "k",
        })
        base, user, token = jc._conf()
        assert base == "https://x.atlassian.net"   # REST lives at the site root
        assert (user, token) == ("u", "k")

    def test_missing_secrets_disable_the_integration(self, monkeypatch):
        monkeypatch.setattr(jc.st, "secrets", {})
        assert jc._conf() is None
        assert jc.available() is False

    def test_story_and_field_reads_degrade_without_jira(self, monkeypatch):
        """AI Test Design must survive an unconfigured Jira: an empty story it
        can report on, never an exception."""
        monkeypatch.setattr(jc.st, "secrets", {})
        story = jc.fetch_story("X-1")
        assert story["key"] == "X-1" and story["summary"] == ""
        assert jc.field_ids_by_name(("Acceptance Criteria",)) == {}

    def test_the_leakage_search_refuses_rather_than_reporting_zero(self, monkeypatch):
        """Deliberately the opposite of the above.  An unconfigured Jira must
        not read as "no production incidents" — that is a number, and a wrong
        one.  The Leakage tab catches this and says the data is unavailable."""
        monkeypatch.setattr(jc.st, "secrets", {})
        with pytest.raises(RuntimeError):
            jc.search_all("project = X", ("summary",))


# ── Report: regression flag join ─────────────────────────────────────────────
class TestRegressionFlag:
    @staticmethod
    def _stub_backlog(monkeypatch, base: pd.DataFrame):
        from src.ui import backlog_tab as bl
        monkeypatch.setattr(
            bl, "_backlog_data",
            lambda: (pd.DataFrame(), {("X", "website"): base.assign(category="automated")}, {}),
        )

    def test_a_failed_baseline_load_raises_instead_of_zeroing_regression(
            self, monkeypatch):
        """The fallback used to be an empty baseline, which flagged every
        automated row as NOT regression: a slide-ready chart reading zero
        regression on every BU.  An error in its place is the honest outcome."""
        from src.ui import backlog_tab as bl

        def _boom():
            raise RuntimeError("backlog unavailable")

        monkeypatch.setattr(bl, "_backlog_data", _boom)
        auto = pd.DataFrame([{"case_id": 1, "country_label": "NL",
                              "device": "Desktop", "bu": "X"}])
        with pytest.raises(RuntimeError):
            rt._add_regression_flag(auto, pd.DataFrame(), "website")

    def test_exact_match_flags_regression(self, monkeypatch):
        base = pd.DataFrame([{"case_id": 1, "country_label": "NL", "device": "Desktop"}])
        self._stub_backlog(monkeypatch, base)
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "NL", "device": "Desktop", "bu": "X"},
            {"case_id": 2, "country_label": "NL", "device": "Desktop", "bu": "X"},
        ])
        out = rt._add_regression_flag(auto, pd.DataFrame(), "website")
        assert list(out["is_regression"]) == [True, False]

    def test_device_less_rows_match_at_case_level(self, monkeypatch):
        base = pd.DataFrame([{"case_id": 1, "country_label": "NL", "device": "Desktop"}])
        self._stub_backlog(monkeypatch, base)
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "ZZ", "device": "Unspecified", "bu": "X"},
        ])
        out = rt._add_regression_flag(auto, pd.DataFrame(), "website")
        assert list(out["is_regression"]) == [True]

    def test_join_never_duplicates_rows(self, monkeypatch):
        """Duplicate baseline keys must not multiply the automated rows."""
        base = pd.DataFrame([
            {"case_id": 1, "country_label": "NL", "device": "Desktop"},
            {"case_id": 1, "country_label": "NL", "device": "Desktop"},   # dupe
        ])
        self._stub_backlog(monkeypatch, base)
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "NL", "device": "Desktop", "bu": "X"},
        ])
        out = rt._add_regression_flag(auto, pd.DataFrame(), "website")
        assert len(out) == 1

    def test_empty_input_is_safe(self, monkeypatch):
        self._stub_backlog(monkeypatch, pd.DataFrame(
            columns=["case_id", "country_label", "device"]))
        assert rt._add_regression_flag(pd.DataFrame(), pd.DataFrame(), "website").empty


# ── release readiness: "how long until it ships" ─────────────────────────────
# ── stability controls: the default must always be selectable ────────────────


# ── Daily freshness ──────────────────────────────────────────────────────────
class TestTheNumbersLoadOncePerDay:
    """Asked for on 2026-09-26: load once, keep it all day.  No timed refresh;
    the first run of a new business day reloads, every other run is instant."""

    def _clock(self, monkeypatch, fr, stamps: list[float]):
        """`stamp()` returns the current entry; a clear pops it, the way
        clearing the real cache makes the next call re-stamp."""
        monkeypatch.setattr(fr, "stamp", lambda: stamps[0])
        cleared: list[int] = []

        def _clear():
            cleared.append(1)
            if len(stamps) > 1:
                stamps.pop(0)

        monkeypatch.setattr(fr, "clear_everything", _clear)
        return cleared

    def test_the_day_turns_over_in_italy_not_in_utc(self):
        """Streamlit Cloud runs on UTC; the team does not.  23:30 UTC on the
        26th is already the 27th in Rome."""
        from datetime import date, datetime, timezone

        from src import freshness as fr
        ts = datetime(2026, 9, 26, 23, 30, tzinfo=timezone.utc).timestamp()
        assert fr.business_day(ts) == date(2026, 9, 27)

    def test_numbers_from_an_earlier_day_are_cleared_once(self, monkeypatch):
        import time

        from src import freshness as fr
        yesterday, now = time.time() - 86400 * 2, time.time()
        cleared = self._clock(monkeypatch, fr, [yesterday, now])
        assert fr.roll_over_if_new_day() is True
        assert fr.roll_over_if_new_day() is False     # restamped: today now
        assert cleared == [1]

    def test_todays_numbers_are_left_alone(self, monkeypatch):
        import time

        from src import freshness as fr
        cleared = self._clock(monkeypatch, fr, [time.time()])
        assert fr.roll_over_if_new_day() is False
        assert cleared == []

    def test_a_morning_rush_clears_exactly_once(self, monkeypatch):
        """A second clear would throw away the download the first one started,
        at a rate limit that makes every request count."""
        import threading
        import time

        from src import freshness as fr
        cleared = self._clock(monkeypatch, fr, [time.time() - 86400 * 2, time.time()])
        threads = [threading.Thread(target=fr.roll_over_if_new_day) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert cleared == [1]

    def test_clearing_means_every_cache_and_a_fresh_warm_up(self, monkeypatch):
        """Not a hand-kept list: two of those had drifted and missed the
        Production Sanity run and the tile downloads."""
        from src import freshness as fr
        from src import testrail_client as tr
        calls: list[str] = []
        monkeypatch.setattr(fr.st.cache_data, "clear", lambda: calls.append("caches"))
        monkeypatch.setattr(tr, "reset_warm_state", lambda: calls.append("warm"))
        fr.clear_everything()
        assert calls == ["caches", "warm"]

    def test_reset_warm_state_lets_the_next_run_prefetch(self, monkeypatch):
        from src import testrail_client as tr
        monkeypatch.setattr(tr, "_WARMED_AT", 12345.0)
        monkeypatch.setattr(tr, "_warm_failures", 3)
        tr.reset_warm_state()
        assert tr._WARMED_AT == 0.0 and tr._warm_failures == 0

    def test_nothing_refreshes_on_a_timer(self):
        """The 15-minute watchdog is gone; nothing may bring it back quietly."""
        import pathlib
        assert "run_every" not in pathlib.Path("app.py").read_text()

    def test_no_dashboard_cache_expires_within_the_day(self):
        """A cache that lapses mid-day turns one fast visit into a slow one.
        Only AI Test Design may be shorter: it reads stories people are
        editing while they generate."""
        import ast
        import pathlib

        from src.freshness import DAY_TTL
        allowed_short = {"fetch_story", "_acceptance_field_ids", "fetch_page"}
        offenders = []
        for f in [pathlib.Path("app.py"), *pathlib.Path("src").rglob("*.py")]:
            for node in ast.walk(ast.parse(f.read_text())):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for dec in node.decorator_list:
                    if not (isinstance(dec, ast.Call) and "cache_data" in ast.unparse(dec.func)):
                        continue
                    kw = {k.arg: k.value for k in dec.keywords}
                    persisted = "persist" in kw
                    if persisted and "ttl" in kw:
                        offenders.append(f"{f}:{node.name} passes a ttl Streamlit ignores")
                    ttl = kw.get("ttl")
                    if (isinstance(ttl, ast.Constant) and ttl.value < DAY_TTL
                            and node.name not in allowed_short):
                        offenders.append(f"{f}:{node.name} ttl={ttl.value}")
                    if ttl is None and not persisted:
                        offenders.append(f"{f}:{node.name} has no ttl and no persist")
        assert not offenders, offenders
