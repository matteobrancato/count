"""Regression tests for the counting rules that produce the dashboard's numbers.

These lock in the business decisions taken with the QA lead — each test states a
rule in the terms it was agreed, so a future refactor that silently changes a
number fails here instead of in front of management.

Run:  pytest -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from src import bu_rules as br
from src.ui import backlog_tab as bl
from src.ui import coverage_tab as cov
from src.ui import styles


# ── fixtures: minimal rule stubs shaped like the real ones ───────────────────
@pytest.fixture
def website_rule():
    return SimpleNamespace(
        bu="Drogas", scope="website", suite_id=1,
        countries_filter=["DRG LV"], country_labels={"DRG LV": "LV"},
        country_field_label="multi_countries",
    )


@pytest.fixture
def microservices_rule():
    return SimpleNamespace(
        bu="Microservices", scope="next_gen", suite_id=9570,
        countries_filter=["MCH"], country_labels={"MCH": "CH"},
        country_field_label="custom_country_coverage",
    )


@pytest.fixture
def mapp_rule():
    return SimpleNamespace(bu="Drogas", scope="mobile_app", suite_id=19110)


def _case(**over):
    """A non-deprecated raw case row with sane defaults."""
    base = {
        "case_id": 1, "suite_id": 1, "labels": ["big_regr_desktop"],
        "type_label": "Functional", "multi_countries": ["DRG LV"],
        "priority_label": "High", "status_Automation Status": "Not automated",
    }
    base.update(over)
    return base


# ── website baseline: device comes from the big_regr labels ──────────────────
class TestWebsiteBaseline:
    def test_both_labels_expand_to_desktop_and_mobile(self, website_rule):
        raw = pd.DataFrame([_case(labels=["big_regr_desktop", "big_regr_mobile"])])
        out = bl._expand_baseline(raw, [website_rule])
        assert sorted(out["device"]) == ["Desktop", "Mobile"]

    def test_single_label_expands_to_one_row(self, website_rule):
        raw = pd.DataFrame([_case(labels=["big_regr_mobile"])])
        out = bl._expand_baseline(raw, [website_rule])
        assert list(out["device"]) == ["Mobile"]

    def test_case_without_baseline_label_is_excluded(self, website_rule):
        raw = pd.DataFrame([_case(labels=[])])
        assert bl._expand_baseline(raw, [website_rule]).empty

    def test_country_without_matching_token_is_excluded(self, website_rule):
        raw = pd.DataFrame([_case(multi_countries=["SOMETHING_ELSE"])])
        assert bl._expand_baseline(raw, [website_rule]).empty


# ── Microservices: API-type cases carry device "API", never Desktop/Mobile ───
class TestMicroservicesApiDevice:
    def test_api_type_collapses_to_single_api_row(self, microservices_rule):
        raw = pd.DataFrame([_case(
            suite_id=9570, type_label="API", country_coverage=["MCH"],
            labels=["big_regr_desktop", "big_regr_mobile"],
        )])
        out = bl._expand_baseline(raw, [microservices_rule])
        assert list(out["device"]) == ["API"]

    def test_non_api_type_keeps_label_devices(self, microservices_rule):
        """A Regression-type case in the microservices suite is NOT forced to API."""
        raw = pd.DataFrame([_case(
            suite_id=9570, type_label="Regression", country_coverage=["MCH"],
            labels=["big_regr_desktop"],
        )])
        out = bl._expand_baseline(raw, [microservices_rule])
        assert list(out["device"]) == ["Desktop"]


# ── Mobile App: priority-based baseline, device = mobile OS ──────────────────
class TestMobileAppBaseline:
    @pytest.mark.parametrize("priority", ["High", "Highest", "highest", " HIGH "])
    def test_high_and_highest_are_in_baseline(self, mapp_rule, priority):
        raw = pd.DataFrame([_case(suite_id=19110, priority_label=priority,
                                  mapp_devices=["iOS"])])
        assert not bl._expand_mapp_baseline(raw, [mapp_rule]).empty

    @pytest.mark.parametrize("priority", ["Medium", "Low", "", None])
    def test_other_priorities_are_excluded(self, mapp_rule, priority):
        raw = pd.DataFrame([_case(suite_id=19110, priority_label=priority,
                                  mapp_devices=["iOS"])])
        assert bl._expand_mapp_baseline(raw, [mapp_rule]).empty

    def test_both_os_expands_to_two_rows(self, mapp_rule):
        raw = pd.DataFrame([_case(suite_id=19110, priority_label="Highest",
                                  mapp_devices=["iOS", "Android"])])
        out = bl._expand_mapp_baseline(raw, [mapp_rule])
        assert sorted(out["device"]) == ["Android", "iOS"]

    def test_country_label_is_the_bu(self, mapp_rule):
        """MAPP has no country dimension — it must match the automated set,
        which emits country_label = rule.bu."""
        raw = pd.DataFrame([_case(suite_id=19110, priority_label="High",
                                  mapp_devices=["iOS"])])
        out = bl._expand_mapp_baseline(raw, [mapp_rule])
        assert list(out["country_label"]) == ["Drogas"]


# ── ICI: LU counts only for Highest-priority cases ───────────────────────────
class TestConditionalLuToken:
    TOKENS = ["IPXL NL", "IPXL BE", "IPXL LU"]

    @pytest.mark.parametrize("priority", ["High", "Medium", "Low", None, ""])
    def test_lu_dropped_for_non_highest(self, priority):
        assert br.filter_conditional_tokens(self.TOKENS, priority) == \
            ["IPXL NL", "IPXL BE"]

    @pytest.mark.parametrize("priority", ["Highest", "4 - Highest", "highest"])
    def test_lu_kept_for_highest(self, priority):
        assert br.filter_conditional_tokens(self.TOKENS, priority) == self.TOKENS

    def test_lu_only_case_disappears_when_not_highest(self):
        assert br.filter_conditional_tokens(["IPXL LU"], "High") == []

    def test_other_bus_tokens_are_never_filtered(self):
        for prio in ("High", "Low", None):
            assert br.filter_conditional_tokens(["WTR", "EE"], prio) == ["WTR", "EE"]


# ── classification: automated wins over the status-derived category ──────────
class TestClassification:
    def test_matching_automated_row_becomes_automated(self):
        expanded = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Desktop",
             "_cat_base": "backlog"},
        ])
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Desktop"},
        ])
        out = bl._classify_expanded(expanded, auto)
        assert list(out["category"]) == ["automated"]

    def test_device_mismatch_does_not_become_automated(self):
        """The bug that made every automated Microservices case show as Unknown:
        baseline said Desktop, the automated set said something else."""
        expanded = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Desktop",
             "_cat_base": "unknown"},
        ])
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Unspecified"},
        ])
        out = bl._classify_expanded(expanded, auto)
        assert list(out["category"]) == ["unknown"]

    def test_empty_automated_set_keeps_base_category(self):
        expanded = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Desktop",
             "_cat_base": "backlog"},
        ])
        out = bl._classify_expanded(expanded, pd.DataFrame())
        assert list(out["category"]) == ["backlog"]


# ── framework / OS breakdown is scope-appropriate ────────────────────────────
class TestBreakdownColumns:
    def test_mapp_stats_expose_ios_and_android(self):
        expanded = pd.DataFrame([
            {"case_id": 1, "country_label": "Drogas", "device": "iOS",
             "category": "automated"},
            {"case_id": 1, "country_label": "Drogas", "device": "Android",
             "category": "automated"},
            {"case_id": 2, "country_label": "Drogas", "device": "iOS",
             "category": "backlog"},
        ])
        s = bl._stats(expanded, pd.DataFrame())
        assert (s["ios"], s["android"]) == (1, 1)   # automated rows only

    def test_website_stats_keep_java_and_testim_at_zero_without_auto(self):
        expanded = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Desktop",
             "category": "backlog"},
        ])
        s = bl._stats(expanded, pd.DataFrame())
        assert (s["java"], s["testim"]) == (0, 0)


# ── RAG thresholds: one colour must mean one thing everywhere ────────────────
class TestRagThresholds:
    @pytest.mark.parametrize("pct,dot", [
        (100.0, "🟢"), (80.0, "🟢"), (79.9, "🟡"), (60.0, "🟡"),
        (59.9, "🔴"), (0.0, "🔴"),
    ])
    def test_coverage_health_boundaries(self, pct, dot):
        assert styles.coverage_health(pct)[0] == dot

    @pytest.mark.parametrize("pct,dot", [
        (0.0, "🟢"), (3.0, "🟢"), (3.1, "🟡"), (6.0, "🟡"), (6.1, "🔴"),
    ])
    def test_backlog_health_boundaries(self, pct, dot):
        assert styles.backlog_health(pct)[0] == dot


# ── section container-chain detection (Coverage area grouping) ───────────────
class TestContainerChain:
    def test_dominant_root_is_stripped(self):
        paths = pd.Series([f"SD > Area{i}" for i in range(10)])
        assert cov._detect_container_chain(paths) == ["SD"]

    def test_balanced_paths_have_no_container(self):
        paths = pd.Series(["Checkout > A", "PIM > B", "Customer > C", "Search > D"])
        assert cov._detect_container_chain(paths) == []

    def test_empty_input_is_safe(self):
        assert cov._detect_container_chain(pd.Series([], dtype=str)) == []
