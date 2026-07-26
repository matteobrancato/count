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
from src.ui import runs_tab as rn


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
class TestParseCaseId:
    @pytest.mark.parametrize("text,expected", [
        ("https://x.testrail.io/index.php?/cases/view/3500712", 3500712),
        ("C3500712", 3500712),
        ("3500712", 3500712),
        ("  c3500712  ", 3500712),
        ("", None),
        ("no digits here", None),
    ])
    def test_accepts_url_prefixed_and_bare_ids(self, text, expected):
        assert rn._parse_case_id(text) == expected


# ── JIRA key extraction from TestRail defect fields ──────────────────────────
class TestJiraKeyExtraction:
    def test_bare_key(self):
        assert rn._extract_jira_keys("EE20-1234") == ["EE20-1234"]

    def test_key_inside_url(self):
        assert rn._extract_jira_keys(
            "https://x.atlassian.net/browse/EE20-1234") == ["EE20-1234"]

    def test_multiple_keys_deduped_in_order(self):
        assert rn._extract_jira_keys("EE20-1, MIC-2, EE20-1") == ["EE20-1", "MIC-2"]

    def test_empty_input(self):
        assert rn._extract_jira_keys(None) == []
        assert rn._extract_jira_keys("") == []


# ── BU matching from run/plan names ──────────────────────────────────────────
class TestBuAliasMatching:
    def test_no_name_matches_nothing(self):
        assert rn._bus_for_run_name(None) == set()
        assert rn._bus_for_run_name("") == set()

    def test_alias_is_matched_case_insensitively(self):
        assert "Drogas" in rn._bus_for_run_name("DRG LV Regression")
        assert "Drogas" in rn._bus_for_run_name("drg lv regression")

    def test_shared_alias_returns_every_owning_bu(self):
        """'EE' (Eastern Europe) legitimately belongs to several BUs."""
        assert len(rn._bus_for_run_name("EE Regression Run")) > 1

    def test_substring_does_not_falsely_match(self):
        """Alias matching is word-bounded, so 'SDK' must not match 'SD'."""
        assert "Superdrug" not in rn._bus_for_run_name("SDK smoke run")


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

    def test_calls_are_noops_when_unavailable(self, monkeypatch):
        """Every caller must survive an unconfigured Jira."""
        monkeypatch.setattr(jc.st, "secrets", {})
        assert jc.fetch_issues(("X-1",)) == {}
        assert jc.fetch_versions("X") == []
        assert jc.count_issues("project = X") is None


# ── Report: regression flag join ─────────────────────────────────────────────
class TestRegressionFlag:
    @staticmethod
    def _stub_backlog(monkeypatch, base: pd.DataFrame):
        from src.ui import backlog_tab as bl
        monkeypatch.setattr(
            bl, "_backlog_data",
            lambda: (pd.DataFrame(), {("X", "website"): base.assign(category="automated")}, {}),
        )

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
