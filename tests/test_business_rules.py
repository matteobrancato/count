"""Regression tests for the counting rules that produce the dashboard's numbers.

These lock in the business decisions taken with the QA lead — each test states a
rule in the terms it was agreed, so a future refactor that silently changes a
number fails here instead of in front of management.

Run:  pytest -q
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pandas as pd
import pytest

from src import bu_rules as br
from src import rules_engine as eng
from src.ui import backlog_tab as bl
from src.ui import coverage_tab as cov
from src.ui import data_quality as dq
from src.ui import styles


# ── fixtures: minimal rule stubs shaped like the real ones ───────────────────
@pytest.fixture
def website_rule():
    return SimpleNamespace(
        bu="Drogas", scope="website", suite_id=1,
        countries_filter=["DRG LV"], country_labels={"DRG LV": "LV"},
        status_field_label="Automation Status",
            country_field_label="multi_countries",
    )


@pytest.fixture
def microservices_rule():
    return SimpleNamespace(
        bu="Microservices", scope="next_gen", suite_id=9570,
        countries_filter=["MCH"], country_labels={"MCH": "CH"},
        status_field_label="Automation Status",
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


# ── the two tabs must never disagree on the same BU ──────────────────────────
class TestCoverageAgreesWithBacklog:
    """Coverage's baseline view and the Backlog tab measure the same thing.

    They used to divide differently — Coverage by unique CASES, Backlog by
    expanded ROWS — so one BU read 91.9% on one tab and 95.2% on the other.
    Coverage now consumes the Backlog's own classified rows, so the agreement
    is structural.  These tests fail if anyone reintroduces a second basis.
    """

    @staticmethod
    def _fixture(website_rule):
        """3 cases × 2 countries, one of them automated on both countries."""
        rule = SimpleNamespace(
            bu="Drogas", scope="website", suite_id=1,
            countries_filter=["DRG LV", "DRG LT"],
            country_labels={"DRG LV": "LV", "DRG LT": "LT"},
            status_field_label="Automation Status",
            country_field_label="multi_countries",
        )
        raw = pd.DataFrame([
            _case(case_id=1, multi_countries=["DRG LV", "DRG LT"],
                  section_path="SD > Checkout"),
            _case(case_id=2, multi_countries=["DRG LV", "DRG LT"],
                  section_path="SD > Checkout"),
            _case(case_id=3, multi_countries=["DRG LV"],
                  section_path="SD > Customer"),
        ])
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Desktop",
             "section_path": "SD > Checkout"},
            {"case_id": 1, "country_label": "LT", "device": "Desktop",
             "section_path": "SD > Checkout"},
        ])
        return raw, auto, [rule]

    def test_headline_rows_and_coverage_match(self, website_rule):
        raw, auto, rules = self._fixture(website_rule)
        # Backlog's numbers
        expanded_bl = bl._classify_expanded(bl._expand_baseline(raw, rules), auto)
        # empty framework frame: the Java/TestIM split is not what's under test
        s = bl._stats(expanded_bl, pd.DataFrame())
        # Coverage's baseline view, from the same inputs
        _nd, _ab, _ids, expanded_cov = cov._baseline_like_backlog(
            raw, auto, rules)

        assert len(expanded_cov) == s["total"] == 5          # 2+2+1 rows
        assert int((expanded_cov["category"] == "automated").sum()) == s["automated"] == 2
        cov_pct = (expanded_cov["category"] == "automated").sum() / len(expanded_cov) * 100
        assert round(cov_pct, 1) == round(s["cov_total"], 1) == 40.0

    def test_per_area_denominator_is_rows_not_cases(self, website_rule):
        raw, auto, rules = self._fixture(website_rule)
        _nd, ab, ids, expanded_cov = cov._baseline_like_backlog(
            raw, auto, rules)
        table, _chain = cov._coverage_table(raw, ab, ids, expanded=expanded_cov)
        checkout = table[table["section"] == "Checkout"].iloc[0]
        # 2 cases × 2 countries = 4 rows (the case basis would have said 2)
        assert int(checkout["total"]) == 4
        assert int(checkout["automated"]) == 2
        assert float(checkout["coverage_pct"]) == 50.0

    def test_without_expanded_frame_the_case_basis_is_kept(self, website_rule):
        """Total / Production Sanity have no row expansion — they must stay on
        the case basis rather than silently borrow the baseline's."""
        raw, auto, rules = self._fixture(website_rule)
        _nd, ab, ids, _exp = cov._baseline_like_backlog(raw, auto, rules)
        table, _chain = cov._coverage_table(raw, ab, ids)
        checkout = table[table["section"] == "Checkout"].iloc[0]
        assert int(checkout["total"]) == 2                   # unique cases
        assert float(checkout["coverage_pct"]) == 50.0       # 1 of 2 cases


# ── Dexter must quote the same numbers the screen shows ──────────────────────
class TestDexterAgreesWithDashboard:
    """The assistant answers from its own snapshot, so its arithmetic has to be
    the dashboard's arithmetic — not merely similar to it."""

    def test_regression_stats_match_the_backlog_tab(self, website_rule):
        raw = pd.DataFrame([
            _case(case_id=1, multi_countries=["DRG LV"]),
            _case(case_id=2, multi_countries=["DRG LV"],
                  **{"status_Automation Status": "Automated"}),
        ])
        auto = pd.DataFrame([
            {"case_id": 2, "country_label": "LV", "device": "Desktop"},
        ])
        expanded = bl._classify_expanded(bl._expand_baseline(raw, [website_rule]), auto)

        from src.ui import chat_assistant as ai
        dexter = ai._regression_stats(expanded)
        tab    = bl._stats(expanded, pd.DataFrame())

        assert dexter["total_rows"]     == tab["total"]
        assert dexter["automated_rows"] == tab["automated"]
        assert dexter["coverage_pct"]   == round(tab["cov_total"], 1) == 50.0

    def test_empty_frame_does_not_divide_by_zero(self):
        from src.ui import chat_assistant as ai
        empty = pd.DataFrame(columns=["case_id", "category"])
        assert ai._regression_stats(empty)["coverage_pct"] == 0.0


# ── Backlog split: work to start vs work to extend ───────────────────────────
class TestBacklogSplit:
    """A case automated for NL but not BE is not the same job as a case nobody
    ever automated.  The first is "Partially Automated", the second "Backlog" —
    and neither may leak into Automated, so Coverage must not move."""

    @staticmethod
    def _rule():
        return SimpleNamespace(
            bu="Drogas", scope="website", suite_id=1,
            countries_filter=["DRG LV", "DRG LT"],
            country_labels={"DRG LV": "LV", "DRG LT": "LT"},
            status_field_label="Automation Status",
            country_field_label="multi_countries",
        )

    def _frames(self):
        rule = self._rule()
        raw = pd.DataFrame([
            # automated on LV only → its LT row is "partially automated"
            _case(case_id=1, multi_countries=["DRG LV", "DRG LT"]),
            # automated nowhere → both rows are real Backlog
            _case(case_id=2, multi_countries=["DRG LV", "DRG LT"]),
        ])
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Desktop"},
        ])
        return bl._classify_expanded(bl._expand_baseline(raw, [rule]), auto)

    def test_row_of_a_case_automated_elsewhere_is_not_backlog(self):
        exp = self._frames()
        cats = dict(zip(zip(exp["case_id"], exp["country_label"]), exp["category"]))
        assert cats[(1, "LV")] == "automated"
        assert cats[(1, "LT")] == "partially_automated"
        assert cats[(2, "LV")] == "backlog"
        assert cats[(2, "LT")] == "backlog"

    def test_coverage_is_unchanged_by_the_split(self):
        s = bl._stats(self._frames(), pd.DataFrame())
        # 1 automated of 4 rows — the split moved nothing into Automated.
        assert s["automated"] == 1
        assert round(s["cov_total"], 1) == 25.0
        # Partially automated rows stay automatable, so this ratio is untouched.
        assert round(s["cov_automatable"], 1) == 25.0

    def test_categories_still_sum_to_the_total(self):
        s = bl._stats(self._frames(), pd.DataFrame())
        assert (s["automated"] + s["backlog"] + s["partially_automated"]
                + s["to_be_updated"] + s["not_applicable"] + s["unknown"]
                ) == s["total"] == 4

    def test_backlog_shrinks_and_the_difference_is_the_new_bucket(self):
        s = bl._stats(self._frames(), pd.DataFrame())
        assert s["backlog"] == 2            # was 3 before the split
        assert s["partially_automated"] == 1


# ── the export behind a tile must match the tile ─────────────────────────────
class TestTileExports:
    """Each tile offers a CSV of the rows behind its number.  If the two ever
    disagree the feature is worse than useless — it hands a QA lead a file that
    contradicts the screen."""

    @staticmethod
    def _setup(monkeypatch):
        rule = SimpleNamespace(
            bu="Drogas", scope="website", suite_id=1,
            countries_filter=["DRG LV", "DRG LT"],
            country_labels={"DRG LV": "LV", "DRG LT": "LT"},
            status_field_label="Automation Status",
            country_field_label="multi_countries",
        )
        raw = pd.DataFrame([
            _case(case_id=1, title="A", section_path="SD > X", url="u1"),
            _case(case_id=2, title="B", section_path="SD > X", url="u2"),
            _case(case_id=3, title="C", section_path="SD > Y", url="u3",
                  **{"status_Automation Status": "Automation not applicable"}),
        ])
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "LV", "device": "Desktop"},
        ])
        monkeypatch.setattr(bl, "_load_scope", lambda scope: (raw, auto, [rule]))
        return bl._classify_expanded(bl._expand_baseline(raw, [rule]), auto)

    def test_every_export_matches_its_tile(self, monkeypatch):
        exp = self._setup(monkeypatch)
        s = bl._stats(exp, pd.DataFrame())
        for cat, _label in bl._EXPORT_CATEGORIES:
            expected = s["total"] if cat == "total" else s.get(cat, 0)
            assert len(bl._category_rows(exp, cat, "website")) == expected, cat

    def test_export_carries_a_testrail_link_per_row(self, monkeypatch):
        exp = self._setup(monkeypatch)
        rows = bl._category_rows(exp, "backlog", "website")
        assert "TestRail Link" in rows.columns
        assert rows["TestRail Link"].notna().all()
        assert rows["Case ID"].str.startswith("C").all()

    def test_empty_category_yields_no_file(self, monkeypatch):
        exp = self._setup(monkeypatch)
        assert bl._category_rows(exp, "to_be_updated", "website").empty
        assert bl._category_rows(pd.DataFrame(), "backlog", "website").empty


class TestExportEvidence:
    """The export carries the TestRail fields the decision was made from, and
    the "Decided By" column must agree with the row's category — quoting
    "Automated UAT" next to a row that is not automated would make the file
    contradict the number it is supposed to justify."""

    @staticmethod
    def _setup(monkeypatch):
        rule = SimpleNamespace(
            bu="ICI Paris XL", scope="website", suite_id=1,
            countries_filter=["IPXL NL", "IPXL BE"],
            country_labels={"IPXL NL": "NL", "IPXL BE": "BE"},
            status_field_label="Automation Status",
            country_field_label="multi_countries",
        )
        raw = pd.DataFrame([
            {"case_id": 1, "suite_id": 1, "labels": ["big_regr_desktop"],
             "type_label": "Functional",
             "multi_countries": ["IPXL NL", "IPXL BE"],
             "country_coverage": ["IPXL NL"], "priority_label": "High",
             "device": "Both", "title": "t", "section_path": "SD",
             "url": "u", "automation_tool": None,
             "status_Automation Status": "Ready to be automated",
             "status_Automation Status Testim Desktop": "Automated UAT",
             "status_Automation Status Testim Mobile View": None},
            {"case_id": 2, "suite_id": 1, "labels": ["big_regr_desktop"],
             "type_label": "Functional",
             "multi_countries": ["IPXL NL", "IPXL BE"],
             "country_coverage": [], "priority_label": "High",
             "device": "Desktop", "title": "t2", "section_path": "SD",
             "url": "u2", "automation_tool": None,
             "status_Automation Status": "Blocked",
             "status_Automation Status Testim Desktop": None,
             "status_Automation Status Testim Mobile View": None},
        ])
        auto = pd.DataFrame([
            {"case_id": 1, "country_label": "NL", "device": "Desktop"},
        ])
        monkeypatch.setattr(bl, "_load_scope", lambda scope: (raw, auto, [rule]))
        return bl._classify_expanded(bl._expand_baseline(raw, [rule]), auto)

    def test_carries_the_testrail_decision_fields(self, monkeypatch):
        rows = bl._category_rows(self._setup(monkeypatch), "total", "website")
        for col in ("Automation Status", "Automation Status Testim Desktop",
                    "Country Coverage", "Countries counted for this BU",
                    "Labels", "Priority", "Type", "Decided By", "Deciding Value"):
            assert col in rows.columns, col

    def test_decision_column_agrees_with_the_category(self, monkeypatch):
        rows = bl._category_rows(self._setup(monkeypatch), "total", "website")
        by = {(r["Case ID"], r["Country"]): (r["Category"], r["Decided By"],
                                             r["Deciding Value"])
              for _, r in rows.iterrows()}
        # automated → the field that actually says "automated"
        cat, field, value = by[("C1", "NL")]
        assert cat == "Automated" and value == "Automated UAT"
        # partially automated → NOT an automated value, but the coverage reason
        cat, field, value = by[("C1", "BE")]
        assert cat == "Partially Automated"
        assert "coverage" in field.lower()
        assert value not in _STATUS_AUTO_FOR_TEST
        # backlog → the blocking status, from the field that carries it
        cat, field, value = by[("C2", "NL")]
        assert (cat, field, value) == ("Backlog", "Automation Status", "Blocked")


_STATUS_AUTO_FOR_TEST = {"Automated", "Automated DEV", "Automated UAT",
                         "Automated Prod"}


# ── Coverage: readable labels and honest TestRail links ──────────────────────
class TestCoverageSectionLinks:
    """A row of the coverage table can span several TestRail sections, because
    the label is the path cut at the chosen depth.  Linking such a row would
    land the reader on an arbitrary one of them, so those rows carry no link."""

    @staticmethod
    def _frame():
        # Two top-level branches, so the container detector strips only "SD":
        # with a single branch it would treat that level as a container too and
        # every row would already be a leaf.
        rows = [{"case_id": i, "section_path": "SD > Content > [DRG] Analytics",
                 "section_id": 11, "suite_id": 7} for i in range(1, 4)]
        rows += [{"case_id": i, "section_path": "SD > Content > [DRG] SEO",
                  "section_id": 12, "suite_id": 7} for i in range(4, 7)]
        rows += [{"case_id": i, "section_path": "SD > Checkout > [DRG] Cart",
                  "section_id": 21, "suite_id": 7} for i in range(7, 10)]
        return pd.DataFrame(rows)

    @staticmethod
    def _patch_base(monkeypatch):
        from types import SimpleNamespace
        from src import testrail_client as tr
        monkeypatch.setattr(
            tr.TestRailCredentials, "from_secrets",
            staticmethod(lambda: SimpleNamespace(base_url="https://x.testrail.io")),
        )

    def test_single_section_rows_get_a_link(self, monkeypatch):
        self._patch_base(monkeypatch)
        table, _chain = cov._coverage_table(
            self._frame(), pd.DataFrame(columns=["section_path", "device"]),
            {1}, depth_offset=2)
        assert (table["section_url"].str.startswith("https://x.testrail.io")).all()
        assert "group_id=11" in " ".join(table["section_url"])

    def test_rows_spanning_several_sections_get_none(self, monkeypatch):
        self._patch_base(monkeypatch)
        # depth 0 collapses Analytics + SEO into one "Content" row (two
        # sections → no link), while "Checkout" is still a single section and
        # keeps its link.  The point is the discrimination, not blanket silence.
        table, _chain = cov._coverage_table(
            self._frame(), pd.DataFrame(columns=["section_path", "device"]),
            {1}, depth_offset=0)
        url = dict(zip(table["section"], table["section_url"]))
        assert url["Content"] == ""
        assert url["Checkout"].endswith("group_id=21")

    def test_area_label_is_the_leaf_of_the_path(self, monkeypatch):
        self._patch_base(monkeypatch)
        table, _chain = cov._coverage_table(
            self._frame(), pd.DataFrame(columns=["section_path", "device"]),
            {1}, depth_offset=2)
        assert {"[DRG] Analytics", "[DRG] SEO", "[DRG] Cart"} <= set(table["area"])
        # the full path survives for sorting, colours and the tooltip
        assert all(" > " in v for v in table["section"])

    def test_chart_uses_the_short_label_and_keeps_its_height(self, monkeypatch):
        self._patch_base(monkeypatch)
        table, _chain = cov._coverage_table(
            self._frame(), pd.DataFrame(columns=["section_path", "device"]),
            {1}, depth_offset=2)
        spec = cov._build_coverage_bar(table, cov._area_color_map(table)).to_dict()
        enc = spec["layer"][0]["encoding"]
        # The short label is a DATA column, not an axis expression: an axis
        # `labelExpr` collapses a step-height chart to zero height (measured:
        # the bars vanished entirely).
        assert enc["y"]["field"] == "_label"
        assert "labelExpr" not in enc["y"]["axis"]
        assert spec["height"] == {"step": 26}
        # No href channel: Streamlit's embed renders no links for it, so a
        # clickable-looking bar would do nothing.
        assert "href" not in enc

    def test_chart_survives_a_frame_without_links(self):
        table = pd.DataFrame([{"section": "A", "total": 2, "desktop": 1,
                               "mobile": 0, "unspecified": 0, "automated": 1,
                               "auto_unique": 1, "coverage_pct": 50.0}])
        cov._build_coverage_bar(table, {"A": "#000"}).to_dict()   # must not raise


class TestFrameworkPrecedence:
    """Playwright is the third generation of tooling (Java → Testim →
    Playwright).  A test can carry more than one, so each row is attributed to
    the NEWEST framework covering it.

    The property that matters: precedence RELABELS rows, it never adds or
    removes them, so Total / Automated / Backlog / Coverage cannot move.
    """

    @staticmethod
    def _raw(*labelled):
        return pd.DataFrame([
            {"case_id": cid, "labels": labels}
            for cid, labels in labelled
        ])

    @staticmethod
    def _auto(*rows):
        return pd.DataFrame([
            {"case_id": cid, "country_label": country,
             "device": device, "framework": fw}
            for cid, country, device, fw in rows
        ])

    def test_label_promotes_java_to_playwright(self):
        out = eng._apply_framework_precedence(
            self._auto((1, "NL", "Desktop", "java")),
            self._raw((1, ["big_regr_desktop", "playwright"])))
        assert list(out["framework"]) == ["playwright"]

    def test_label_matched_case_insensitively(self):
        out = eng._apply_framework_precedence(
            self._auto((1, "NL", "Desktop", "testim_desktop")),
            self._raw((1, ["Playwright"])))
        assert list(out["framework"]) == ["playwright"]

    def test_without_the_label_nothing_moves(self):
        auto = self._auto((1, "NL", "Desktop", "java"),
                          (2, "BE", "Desktop", "testim_desktop"))
        out  = eng._apply_framework_precedence(auto, self._raw((1, ["big_regr_desktop"])))
        assert list(out["framework"]) == ["java", "testim_desktop"]

    def test_testim_wins_over_java_on_the_same_row(self):
        out = eng._apply_framework_precedence(
            self._auto((1, "NL", "Desktop", "java"),
                       (1, "NL", "Desktop", "testim_desktop")),
            self._raw((1, ["big_regr_desktop"])))
        assert set(out["framework"]) == {"testim_desktop"}

    def test_precedence_is_per_row_not_per_case(self):
        """A Mobile row that only Java covers stays Java even when the case is
        also automated with Testim on Desktop — otherwise the Mobile framework
        split would credit a tool that never ran there."""
        out = eng._apply_framework_precedence(
            self._auto((1, "NL", "Desktop", "java"),
                       (1, "NL", "Desktop", "testim_desktop"),
                       (1, "NL", "Mobile",  "java")),
            self._raw((1, ["big_regr_desktop", "big_regr_mobile"])))
        by_device = dict(zip(out["device"], out["framework"]))
        assert by_device == {"Desktop": "testim_desktop", "Mobile": "java"}

    def test_mobile_app_is_never_relabelled(self):
        """Mobile App has its own tooling field; a stray label must not move it
        into the website framework split."""
        out = eng._apply_framework_precedence(
            self._auto((1, "NL", "iOS", "mobile_app")),
            self._raw((1, ["playwright"])))
        assert list(out["framework"]) == ["mobile_app"]

    def test_row_count_and_membership_are_untouched(self):
        auto = self._auto((1, "NL", "Desktop", "java"),
                          (1, "NL", "Desktop", "testim_desktop"),
                          (2, "BE", "Mobile",  "testim_mobile"),
                          (3, "FR", "Desktop", "java"))
        out  = eng._apply_framework_precedence(
            auto, self._raw((1, ["playwright"]), (2, ["big_regr_mobile"])))
        assert len(out) == len(auto)
        keys = ["case_id", "country_label", "device"]
        assert (out[keys].values == auto[keys].values).all()

    def test_empty_inputs_are_safe(self):
        assert eng._apply_framework_precedence(
            pd.DataFrame(), pd.DataFrame()).empty


class TestPlaywrightMigrationCheck:
    """The label makes a case count as Playwright; a STATUS field is what makes
    it count as automated.  When the two disagree the case is invisible
    everywhere else, so the hygiene panel is the only place it can surface."""

    @staticmethod
    def _raw(rows):
        return pd.DataFrame(rows)

    def test_flags_label_without_automated_status(self):
        out = dq._playwright_migration(self._raw([
            {"case_id": 1, "title": "t", "labels": ["playwright"], "url": "",
             "status_Automation Status": "To be automated"}]))
        assert list(out["case_id"]) == [1]
        assert "no automated status" in out.iloc[0]["problem"]

    def test_flags_leftover_testim_status(self):
        out = dq._playwright_migration(self._raw([
            {"case_id": 2, "title": "t", "labels": ["playwright"], "url": "",
             "status_Automation Status": "Automated",
             "status_Automation Status Testim Desktop": "Automated"}]))
        assert list(out["case_id"]) == [2]
        assert "Testim" in out.iloc[0]["problem"]

    def test_clean_migration_is_not_flagged(self):
        assert dq._playwright_migration(self._raw([
            {"case_id": 3, "title": "t", "labels": ["playwright"], "url": "",
             "status_Automation Status": "Automated",
             "status_Automation Status Testim Desktop": None}])).empty

    def test_silent_until_the_migration_starts(self):
        assert dq._playwright_migration(self._raw([
            {"case_id": 4, "title": "t", "labels": ["big_regr_desktop"],
             "url": "", "status_Automation Status": "Automated"}])).empty


class TestFrameworkCardSelection:
    """Only frameworks that carry rows get a card — a tile reading 0 is a label
    that lies, and Watsons (100% TestIM) had one sitting next to every number."""

    @staticmethod
    def _s(java=0, testim=0, playwright=0):
        return {"java": java, "u_java": java,
                "testim": testim, "u_testim": testim,
                "playwright": playwright, "u_playwright": playwright}

    def test_zero_frameworks_are_dropped(self):
        assert [n for n, _, _ in bl._framework_cards(self._s(testim=1336))] == ["TestIM"]

    def test_all_three_when_all_present(self):
        assert [n for n, _, _ in
                bl._framework_cards(self._s(java=5, testim=3, playwright=2))] == [
                    "Java", "TestIM", "Playwright"]

    def test_order_is_oldest_to_newest(self):
        assert [n for n, _, _ in
                bl._framework_cards(self._s(java=1, playwright=1))] == ["Java", "Playwright"]

    def test_mobile_app_shows_no_framework_section(self):
        """MAPP rows carry the `mobile_app` framework, so all three counters are
        zero — the caller drops the whole section rather than print zeros."""
        assert bl._framework_cards(self._s()) == []

    def test_counts_are_carried_through(self):
        assert bl._framework_cards({"java": 0, "u_java": 0,
                                    "testim": 1336, "u_testim": 731,
                                    "playwright": 0, "u_playwright": 0}) == [
            ("TestIM", 1336, 731)]


class TestSummaryTableScopeColumn:
    """The scope radio already filters the table to one scope, so the Scope
    column repeats one value on every row.  It is dropped in that case and kept
    only when a view genuinely mixes scopes."""

    @staticmethod
    def _df(scopes):
        return pd.DataFrame([
            {"BU": f"BU{i}", "Scope": sc, "Total": 10, "Automated": 5,
             "Backlog": 2, "Coverage %": 50.0}
            for i, sc in enumerate(scopes)
        ])

    _NUM = ["Total", "Automated", "Backlog"]

    def test_single_scope_drops_the_column(self):
        out = bl._summary_table_html(self._df(["Website", "Website"]), self._NUM)
        assert "scope-pill" not in out
        assert ">Scope<" not in out

    def test_mixed_scopes_keep_the_column(self):
        out = bl._summary_table_html(
            self._df(["Website", "Microservices"]), self._NUM)
        assert out.count("scope-pill") == 2
        assert ">Scope<" in out

    def test_header_and_body_cell_counts_agree(self):
        """A dropped header with a kept cell (or the reverse) shifts every number
        one column to the side — the worst possible failure for this table."""
        for scopes in (["Website", "Website"], ["Website", "Microservices"]):
            out  = bl._summary_table_html(self._df(scopes), self._NUM)
            head, body = out.split("<tbody>")
            # "<th" also matches inside "<thead" — anchor on the tag boundary.
            n_th = len(re.findall(r"<th[ >]", head))
            for row in body.split("<tr")[1:]:
                assert row.count("<td") == n_th


class TestUnknownRowsOfAutomatedCase:
    """The status field is per CASE, the country coverage per COUNTRY.  A case
    automated in 3 of its 5 countries leaves 2 rows the case-level field cannot
    describe: it says "Automated", which is not a backlog value, so they used to
    fall through to Unknown.  They are Partially Automated — the case IS
    automated, just not there.

    Modelled on the real Marionnaud case: baseline RO/IT/CZ/SK/HU from
    multi_countries, automated CZ/SK/HU from Java Country Coverage.
    """

    @staticmethod
    def _expanded(countries, base="unknown"):
        return pd.DataFrame([
            {"case_id": 1, "country_label": c, "device": "Desktop",
             "_cat_base": base} for c in countries
        ])

    @staticmethod
    def _auto(countries):
        return pd.DataFrame([
            {"case_id": 1, "country_label": c, "device": "Desktop"}
            for c in countries
        ])

    def test_uncovered_countries_become_partially_automated(self):
        out = bl._classify_expanded(
            self._expanded(["RO", "IT", "CZ", "SK", "HU"]),
            self._auto(["CZ", "SK", "HU"]))
        by_country = dict(zip(out["country_label"], out["category"]))
        assert by_country == {"CZ": "automated", "SK": "automated",
                              "HU": "automated",
                              "RO": "partially_automated",
                              "IT": "partially_automated"}

    def test_unknown_survives_when_the_case_is_automated_nowhere(self):
        """Kruidvat / Watsons shape: the automated set is empty for this case, so
        nothing explains the rows and Unknown is the honest answer."""
        out = bl._classify_expanded(
            self._expanded(["BE", "NL"]),
            pd.DataFrame(columns=["case_id", "country_label", "device"]))
        assert set(out["category"]) == {"unknown"}

    def test_backlog_split_still_works(self):
        out = bl._classify_expanded(
            self._expanded(["BE", "NL"], base="backlog"), self._auto(["NL"]))
        by_country = dict(zip(out["country_label"], out["category"]))
        assert by_country == {"NL": "automated", "BE": "partially_automated"}

    def test_other_categories_are_untouched(self):
        """N/A and To Update must NOT be swallowed by the split — they are
        decisions, not gaps."""
        exp = pd.DataFrame([
            {"case_id": 1, "country_label": "NL", "device": "Desktop",
             "_cat_base": "unknown"},
            {"case_id": 1, "country_label": "BE", "device": "Desktop",
             "_cat_base": "not_applicable"},
            {"case_id": 1, "country_label": "FR", "device": "Desktop",
             "_cat_base": "to_be_updated"},
        ])
        out = bl._classify_expanded(exp, self._auto(["NL"]))
        by_country = dict(zip(out["country_label"], out["category"]))
        assert by_country == {"NL": "automated", "BE": "not_applicable",
                              "FR": "to_be_updated"}

    def test_total_still_equals_the_sum_of_categories(self):
        exp = self._expanded(["RO", "IT", "CZ", "SK", "HU"])
        out = bl._classify_expanded(exp, self._auto(["CZ", "SK", "HU"]))
        assert len(out) == len(exp)
        assert int(out["category"].value_counts().sum()) == 5


class TestCoverageExcludingPartial:
    """Third figure on the detail line: Coverage with the partial gaps taken out
    of the baseline.  The Backlog stays in — a test nobody ever automated is
    real missing coverage, not a gap in an existing script."""

    _COLS = ["case_id", "country_label", "device", "category"]

    @classmethod
    def _expanded(cls, cats):
        return pd.DataFrame([
            {"case_id": i, "country_label": "NL", "device": "Desktop",
             "category": c} for i, c in enumerate(cats)
        ], columns=cls._COLS)

    def _stats(self, cats):
        return bl._stats(self._expanded(cats),
                         pd.DataFrame(columns=bl._AUTO_SLIM_COLS))

    def test_partial_gaps_leave_the_denominator(self):
        # 1 automated, 1 partially, 2 backlog → 1/3, not 1/4
        s = self._stats(["automated", "partially_automated",
                         "backlog", "backlog"])
        assert s["cov_total"] == pytest.approx(25.0)
        assert s["cov_ex_partial"] == pytest.approx(100 / 3)

    def test_backlog_still_counts_in_full(self):
        """Trekpleister's shape: no partial gaps, huge backlog — the figure must
        NOT flatter it."""
        s = self._stats(["automated"] + ["backlog"] * 3)
        assert s["cov_ex_partial"] == pytest.approx(s["cov_total"])

    def test_matches_the_marionnaud_numbers(self):
        cats = (["automated"] * 3421 + ["backlog"] * 240
                + ["partially_automated"] * 1419 + ["to_be_updated"] * 80
                + ["not_applicable"] * 14 + ["unknown"] * 604)
        s = self._stats(cats)
        assert s["total"] == 5778
        assert s["cov_total"]      == pytest.approx(59.2, abs=0.05)
        assert s["cov_ex_partial"] == pytest.approx(78.5, abs=0.05)

    def test_no_division_by_zero(self):
        """A BU with a declared baseline but no rows in it."""
        assert self._stats([])["cov_ex_partial"] == 0.0


class TestDeferredTileDownloads:
    """The CSV is produced by a callable Streamlit runs only on click, instead
    of six ready-made files held in memory per BU.  Two things must survive
    that move: the bytes, and the row count in the button's label — which now
    comes from the tile's own number rather than from counting newlines in a
    file that no longer exists at render time.
    """

    @staticmethod
    def _setup(monkeypatch):
        rule = SimpleNamespace(
            bu="Drogas", scope="website", suite_id=1,
            countries_filter=["DRG LV", "DRG LT"],
            country_labels={"DRG LV": "LV", "DRG LT": "LT"},
            status_field_label="Automation Status",
            country_field_label="multi_countries",
        )
        raw = pd.DataFrame([
            _case(case_id=1, title="A", section_path="SD > X", url="u1"),
            _case(case_id=2, title="B", section_path="SD > X", url="u2"),
            _case(case_id=3, title="C", section_path="SD > Y", url="u3",
                  **{"status_Automation Status": "Automation not applicable"}),
        ])
        auto = pd.DataFrame([{"case_id": 1, "country_label": "LV",
                              "device": "Desktop"}])
        monkeypatch.setattr(bl, "_load_scope", lambda scope: (raw, auto, [rule]))
        exp = bl._classify_expanded(bl._expand_baseline(raw, [rule]), auto)
        return exp, bl._evidence_frame(exp, "website")

    def test_nothing_is_serialised_until_the_callable_runs(self, monkeypatch):
        _exp, ev = self._setup(monkeypatch)
        calls = []
        original = pd.DataFrame.to_csv

        def _spy(self, *a, **k):
            calls.append(1)
            return original(self, *a, **k)

        monkeypatch.setattr(pd.DataFrame, "to_csv", _spy)
        writer = bl._csv_writer(ev, "backlog")
        assert calls == []          # building the writer must cost nothing
        writer()
        assert calls == [1]

    def test_bytes_match_the_eager_implementation(self, monkeypatch):
        _exp, ev = self._setup(monkeypatch)
        for cat, _label in bl._EXPORT_CATEGORIES:
            sub = ev if cat == "total" else ev[ev["_cat"] == cat]
            eager = sub.drop(columns=["_cat"]).to_csv(index=False).encode("utf-8")
            assert bl._csv_writer(ev, cat)() == eager, cat

    def test_row_count_equals_the_tile_number(self, monkeypatch):
        """The label says "Download the N rows behind X"; N is now the tile's
        own figure, so it has to equal the rows actually in the file."""
        exp, ev = self._setup(monkeypatch)
        s = bl._stats(exp, pd.DataFrame())
        for cat, _label in bl._EXPORT_CATEGORIES:
            tile_n = s["total"] if cat == "total" else s.get(cat, 0)
            if not tile_n:
                continue
            payload = bl._csv_writer(ev, cat)()
            assert payload.count(b"\n") - 1 == tile_n, cat

    def test_the_frame_is_captured_not_looked_up(self, monkeypatch):
        """The callable runs outside the script run, so it must not depend on a
        cache lookup that may miss there."""
        _exp, ev = self._setup(monkeypatch)
        writer = bl._csv_writer(ev, "backlog")

        def _boom(*a, **k):                      # any data reload would raise
            raise AssertionError("the writer reloaded the pipeline on click")

        monkeypatch.setattr(bl, "_backlog_data", _boom)
        monkeypatch.setattr(bl, "_load_scope", _boom)
        assert writer()                          # must still produce the file


class TestPlaywrightLabelGate:
    """Four BUs (Kruidvat, Trekpleister, Marionnaud, Watsons) do NOT read the
    generic "Automation Status" — their automation lives in BU-specific fields.
    A Playwright case, which is marked with that generic field plus the
    `playwright` label, therefore needs a rule of its own on those BUs, and that
    rule MUST be gated on the label: without the gate it would also admit legacy
    cases whose generic field is filled but whose automation the BU never ran.
    """

    @staticmethod
    def _reg():
        return SimpleNamespace(
            field=lambda lbl: (SimpleNamespace(system_name="custom_auto",
                                               values_by_id={1: "Automated"})
                               if lbl == "Automation Status" else None),
            status_value_ids=lambda lbl, vals: {1},
            type_id=lambda t: None,
            priority_id_to_label={},
        )

    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        monkeypatch.setattr(eng, "_is_deprecated", lambda case, reg: bool(case.get("dep")))
        monkeypatch.setattr(eng, "_get_country_tokens",
                            lambda case, reg, fld, pid=None: case.get("mc", []))

    def _match(self, monkeypatch, labels, case=None, project_id=1):
        monkeypatch.setattr(eng, "_get_labels", lambda c, pid: labels)
        rule = next(r for r in br.ALL_RULES if r.name == "Kruidvat PLAYWRIGHT")
        return eng._rule_matches(case or {"custom_auto": 1, "mc": ["KVBE"]},
                                 rule, self._reg(), project_id=project_id)

    def test_labelled_case_is_automated(self, monkeypatch):
        ok, tokens = self._match(monkeypatch, ["big_regr_desktop", "playwright"])
        assert ok and tokens == ["KVBE"]

    def test_legacy_case_without_the_label_is_rejected(self, monkeypatch):
        """The Kruidvat case that started all of this: "Automation Status" =
        Automated, but that field is not what automates a KV case."""
        ok, _ = self._match(monkeypatch, ["big_regr_desktop"])
        assert not ok

    def test_label_is_matched_case_insensitively(self, monkeypatch):
        ok, _ = self._match(monkeypatch, ["Playwright"])
        assert ok

    def test_gate_fails_closed_without_a_project(self, monkeypatch):
        """If labels cannot be resolved we reject: admitting a case whose
        membership we could not verify is the harmful direction."""
        ok, _ = self._match(monkeypatch, ["playwright"], project_id=None)
        assert not ok

    def test_rules_without_the_gate_never_resolve_labels(self, monkeypatch):
        """Regression guard: every other rule goes through the same matcher and
        must behave exactly as before — no label lookup, no new rejection."""
        def _boom(*a, **k):
            raise AssertionError("a rule with no labels_filter resolved labels")

        monkeypatch.setattr(eng, "_get_labels", _boom)
        rule = next(r for r in br.ALL_RULES if r.name == "KV JAVA")
        reg  = SimpleNamespace(
            field=lambda lbl: (SimpleNamespace(system_name="custom_auto",
                                               values_by_id={1: "Automated"})
                               if lbl == "Automation Status KV SPR" else None),
            status_value_ids=lambda lbl, vals: {1},
            type_id=lambda t: None, priority_id_to_label={},
        )
        ok, _ = eng._rule_matches({"custom_auto": 1, "mc": ["KVBE"]}, rule,
                                  reg, project_id=1)
        assert ok

    def test_every_gapped_bu_has_a_playwright_rule(self):
        """The four BUs whose rules never read "Automation Status" are exactly
        the ones that need a Playwright rule — and no other BU gets a duplicate.
        """
        website = [r for r in br.ALL_RULES if r.scope != "mobile_app"]
        reads_generic = {r.bu for r in website
                         if r.status_field_label == "Automation Status"
                         and not r.labels_filter}
        gapped = {r.bu for r in website} - reads_generic
        assert {r.bu for r in website if r.framework == "playwright"} == gapped

    def test_playwright_rules_gate_on_the_label_and_match_their_siblings(self):
        for r in br.ALL_RULES:
            if r.framework != "playwright":
                continue
            assert r.labels_filter == [br.PLAYWRIGHT_LABEL], r.name
            siblings = {t for s in br.ALL_RULES if s.bu == r.bu
                        and s.framework != "playwright" and s.scope != "mobile_app"
                        for t in s.countries_filter}
            assert set(r.countries_filter) == siblings, r.name

    def test_which_country_field_each_playwright_rule_reads(self):
        """`multi_countries` is the field the BASELINE is expanded from, so a
        Playwright rule reading it can never produce an automated row without a
        baseline row to land on.

        Marionnaud is the deliberate exception: its countries come from
        per-framework coverage fields, which NARROW multi_countries rather than
        replace it — the case is scoped to a country, the coverage field says
        whether automation actually runs there.  A token present in the coverage
        field but absent from multi_countries is therefore a data-entry error,
        not a row: it has no baseline to attach to and Data Quality is where it
        surfaces.  Spelled out per BU so that changing one is a decision, not a
        side effect."""
        assert {r.bu: r.country_field_label for r in br.ALL_RULES
                if r.framework == "playwright"} == {
            "Kruidvat":     "multi_countries",
            "Trekpleister": "multi_countries",
            "Watsons Turkey":  "multi_countries",
            "Watsons Ukraine": "multi_countries",
            "Marionnaud":   "Playwright Country Coverage",
        }


class TestPlaywrightCountryCoverageResolves:
    """The rule names the field by its UI label; TestRail may answer to either
    that or to the system name it was created under
    (`custom_playwright_country_coverage`).  Betting the rule on exactly one of
    them is what silently emptied "Smaller NR", and an empty country field here
    means every Marionnaud Playwright row quietly disappears."""

    @staticmethod
    def _reg(known_as: str):
        from src.field_resolver import FieldMeta, FieldRegistry, _norm
        meta = FieldMeta(system_name="custom_playwright_country_coverage",
                         label="Playwright Country Coverage", type_id=12,
                         values_by_id={7: "MFR", 8: "MCH"}, ids_by_label={})
        reg = FieldRegistry()
        if known_as == "label":
            reg.fields_by_label[_norm("Playwright Country Coverage")] = meta
        else:
            reg.fields_by_system["custom_playwright_country_coverage"] = meta
        return reg

    @pytest.mark.parametrize("known_as", ["label", "system"])
    def test_the_tokens_come_back_either_way(self, known_as):
        case = {"custom_playwright_country_coverage": [7, 8]}
        assert eng._get_country_tokens(
            case, self._reg(known_as), "Playwright Country Coverage") == ["MFR", "MCH"]

    def test_an_unknown_field_yields_no_tokens_rather_than_raising(self):
        from src.field_resolver import FieldRegistry
        assert eng._get_country_tokens(
            {"custom_playwright_country_coverage": [7]},
            FieldRegistry(), "Playwright Country Coverage") == []


class TestEveryBUIsWiredIntoEverySurface:
    """Adding a BU means touching more than bu_rules: two hardcoded display
    orders and the run-alias table all key off the BU NAME, in three different
    files.  Miss one and nothing errors — the BU just silently sorts to the
    bottom of a tab, or its Runs view comes back empty.  These are the guards
    that turn that into a failing test instead."""

    def test_the_global_filter_lists_every_website_bu(self):
        from src.ui.global_filter import _BU_ORDER
        assert set(br.WEBSITE_BUS) <= set(_BU_ORDER), \
            sorted(set(br.WEBSITE_BUS) - set(_BU_ORDER))

    def test_the_report_lists_every_website_bu(self):
        from src.ui.report_tab import _BU_ORDER
        assert set(br.WEBSITE_BUS) <= set(_BU_ORDER), \
            sorted(set(br.WEBSITE_BUS) - set(_BU_ORDER))

    def test_every_website_bu_can_be_matched_to_its_runs(self):
        assert set(br.WEBSITE_BUS) <= set(br.BU_RUN_ALIASES), \
            sorted(set(br.WEBSITE_BUS) - set(br.BU_RUN_ALIASES))

    def test_no_display_order_names_a_bu_that_does_not_exist(self):
        """A stale name is how a rename half-lands: the old entry keeps its
        slot and the renamed BU falls to the end."""
        from src.ui.global_filter import _BU_ORDER as gf
        from src.ui.report_tab import _BU_ORDER as rt
        known = set(br.WEBSITE_BUS) | {r.bu for r in br.ALL_RULES}
        assert set(gf) <= known, sorted(set(gf) - known)
        assert set(rt) <= known, sorted(set(rt) - known)


class TestWatsonsUkraineIsItsOwnBU:
    """Watsons Ukraine is a BU, not a country of Watsons Turkey.  The thing
    that must never happen is the two pooling: separate suites are what keeps
    their baselines, their coverage and their runs apart."""

    def test_it_is_a_website_bu(self):
        assert "Watsons Ukraine" in br.WEBSITE_BUS

    def test_it_shares_no_suite_with_turkey(self):
        ua = {r.suite_id for r in br.rules_for_bu("Watsons Ukraine")}
        tr_ = {r.suite_id for r in br.rules_for_bu("Watsons Turkey")}
        assert ua == {39694}
        assert not (ua & tr_)

    def test_testim_and_playwright_only(self):
        assert {r.framework for r in br.rules_for_bu("Watsons Ukraine", "website")} == \
            {"testim_desktop", "testim_mobile", "playwright"}

    def test_playwright_reads_the_shared_field_behind_the_label(self):
        pw = next(r for r in br.rules_for_bu("Watsons Ukraine")
                  if r.framework == "playwright")
        assert pw.status_field_label == "Automation Status"
        assert pw.labels_filter == [br.PLAYWRIGHT_LABEL]

    def test_the_country_is_a_UA_token_in_multi_countries(self):
        """EVERY rule, TestIM included.  `_testim_pair` defaults to "Testim
        Country Coverage" — correct for Turkey, wrong here: Ukraine carries its
        country in multi_countries, and the default would have hunted for UA in
        a field that does not hold it, leaving every TestIM case un-automated."""
        for r in br.rules_for_bu("Watsons Ukraine", "website"):
            assert r.countries_filter == ["UA"], r.name
            assert r.country_labels == {"UA": "UA"}, r.name
            assert r.country_field_label == "multi_countries", r.name

    def test_UA_reaches_the_screen_with_a_country_name(self):
        """Every other BU shows a country name; a bare code would read as a bug."""
        from src.ui.backlog_tab import COUNTRY_NAMES
        assert COUNTRY_NAMES.get("UA") == "Ukraine"

    def test_its_run_alias_cannot_claim_another_BUs_runs(self):
        """"EE" is shared by three BUs on purpose; Ukraine must not join them
        by accident, and a bare "UA" would match unrelated run names."""
        assert br.BU_RUN_ALIASES["Watsons Ukraine"] == ["WUA"]

    def test_turkey_kept_its_suite_and_aliases_through_the_rename(self):
        assert {r.suite_id for r in br.rules_for_bu("Watsons Turkey", "website")} == {7544}
        assert br.BU_RUN_ALIASES["Watsons Turkey"] == ["WTR", "EE"]


class TestMarionnaudUnifiedStatusField:
    """MRN folded its four per-country-group status fields into the shared
    "Automation Status" (2026-08).  What used to separate the frameworks was a
    field each; it is now a label each, with the countries each framework
    actually covers read from its own coverage field.  TestIM was never used on
    this BU."""

    def _mrn(self):
        return {r.framework: r for r in br.rules_for_bu("Marionnaud", "website")}

    def test_the_two_frameworks_and_nothing_else(self):
        assert set(self._mrn()) == {"java", "playwright"}

    def test_both_read_the_one_unified_status_field(self):
        for fw, rule in self._mrn().items():
            assert rule.status_field_label == "Automation Status", fw

    def test_the_label_is_the_only_thing_telling_them_apart(self):
        mrn = self._mrn()
        assert mrn["java"].labels_filter == [br.JAVA_LABEL]
        assert mrn["playwright"].labels_filter == [br.PLAYWRIGHT_LABEL]
        # Same field, same automated values: without the gate each rule would
        # sweep in the other framework's cases wholesale.
        assert mrn["java"].automated_values == mrn["playwright"].automated_values

    def test_each_framework_reads_its_own_country_coverage(self):
        mrn = self._mrn()
        assert mrn["java"].country_field_label == "Java Country Coverage"
        assert mrn["playwright"].country_field_label == "Playwright Country Coverage"

    def test_no_country_fallback(self):
        """The coverage field IS the statement of where automation runs.  A
        fallback to multi_countries would report a country as automated on the
        strength of the case merely being scoped to it."""
        for fw, rule in self._mrn().items():
            assert rule.country_fallback_field_label is None, fw

    def test_both_cover_every_marionnaud_country(self):
        """One field and one country source per framework means one rule per
        framework — the MFR / SPR split went with the fields that caused it."""
        mrn = self._mrn()
        assert set(mrn["java"].countries_filter) == set(mrn["playwright"].countries_filter)
        assert {mrn["java"].country_labels[t] for t in mrn["java"].countries_filter} == {
            "FR", "CH", "AT", "RO", "IT", "CZ", "SK", "HU"}


class TestToUpdateBeatsAutomated:
    """The tool fields say whether a script EXISTS; "To be updated" is written
    when the test itself changed.  A script that no longer matches its test is
    work to do, not coverage — so the flag wins, whichever field carries it.

    Modelled on the ICI reconciliation: 57 cases whose `Automation Status` says
    "To be updated" while Testim has them automated.  The QA lead's TestRail
    filter counted them, the dashboard did not.
    """

    @staticmethod
    def _exp(rows):
        return pd.DataFrame([
            {"case_id": i, "country_label": "NL", "device": "Desktop",
             "_cat_base": b} for i, b in enumerate(rows, start=1)
        ])

    @staticmethod
    def _auto(ids):
        return pd.DataFrame([{"case_id": i, "country_label": "NL",
                              "device": "Desktop"} for i in ids])

    def test_flagged_row_stays_to_update_even_when_automated(self):
        out = bl._classify_expanded(self._exp(["to_be_updated"]), self._auto([1]))
        assert out["category"].tolist() == ["to_be_updated"]

    def test_automated_still_wins_over_everything_else(self):
        out = bl._classify_expanded(
            self._exp(["unknown", "backlog", "not_applicable"]),
            self._auto([1, 2, 3]))
        assert set(out["category"]) == {"automated"}

    def test_unautomated_flagged_row_is_unchanged(self):
        out = bl._classify_expanded(self._exp(["to_be_updated"]), self._auto([]))
        assert out["category"].tolist() == ["to_be_updated"]

    def test_maintenance_is_still_visible(self):
        """Moving the row out of Automated must not lose the fact that a script
        exists — otherwise To Update cannot tell maintenance from new work."""
        out = bl._classify_expanded(
            self._exp(["to_be_updated", "to_be_updated"]), self._auto([1]))
        s = bl._stats(out, pd.DataFrame(columns=bl._AUTO_SLIM_COLS))
        assert s["to_be_updated"] == 2
        assert s["tbu_automated"] == 1

    def test_total_still_equals_the_sum_of_categories(self):
        out = bl._classify_expanded(
            self._exp(["to_be_updated", "unknown", "backlog", "not_applicable"]),
            self._auto([1, 2]))
        s = bl._stats(out, pd.DataFrame(columns=bl._AUTO_SLIM_COLS))
        assert s["total"] == 4
        assert (s["automated"] + s["backlog"] + s["partially_automated"]
                + s["to_be_updated"] + s["not_applicable"] + s["unknown"]) == 4

    def test_coverage_falls_and_automatable_denominator_does_not(self):
        """The row moves between two categories that were BOTH automatable, so
        only the numerator changes — Coverage drops, the denominator holds."""
        before = bl._classify_expanded(self._exp(["unknown"] * 4), self._auto([1, 2, 3, 4]))
        after  = bl._classify_expanded(
            self._exp(["to_be_updated"] + ["unknown"] * 3), self._auto([1, 2, 3, 4]))
        empty  = pd.DataFrame(columns=bl._AUTO_SLIM_COLS)
        b, a = bl._stats(before, empty), bl._stats(after, empty)
        assert b["cov_total"] == 100.0 and a["cov_total"] == 75.0
        assert b["total"] == a["total"] == 4
        # automatable = auto + backlog + partial + to-update — unchanged
        assert a["automated"] + a["to_be_updated"] == b["automated"]

    def test_stats_survives_a_frame_without_the_flag(self):
        """`_stats` is also called on frames built without `_classify_expanded`
        (Coverage tab, tests) — it must not require the column."""
        df = pd.DataFrame([{"case_id": 1, "country_label": "NL",
                            "device": "Desktop", "category": "to_be_updated"}])
        assert bl._stats(df, pd.DataFrame(columns=bl._AUTO_SLIM_COLS))["tbu_automated"] == 0


class TestCoverageExPartialInSummary:
    """In the All-BU table the bar, the colour and the big figure are on
    coverage EXCLUDING the partial gaps; the coverage over the whole baseline —
    the one every other surface shows — sits underneath in grey.

    The header names the big number, so neither can be misread, and the grey
    line is what lets a reader reconcile the table with the KPI strip."""

    @staticmethod
    def _df(rows):
        out = []
        for bu, total, auto, partial in rows:
            out.append({"BU": bu, "Scope": "Website", "Total": total,
                        "Automated": auto, "Backlog": total - auto - partial,
                        "Partially Automated": partial,
                        "Coverage %": round(auto / total * 100, 1),
                        "Coverage excl. Partially %":
                            round(auto / (total - partial) * 100, 1)})
        return pd.DataFrame(out)

    _NUM = ["Total", "Automated", "Backlog", "Partially Automated"]

    def test_the_bar_is_on_the_ex_partial_figure(self):
        html_ = self._html([("ICI", 5713, 4472, 318)])
        bar = html_[html_.index("cov-fill"):html_.index("cov-ex")]
        assert "82.9%" in bar and "78.3%" not in bar

    def test_the_whole_baseline_figure_stays_visible(self):
        """Without it the table could not be reconciled with the KPI strip."""
        html_ = self._html([("ICI", 5713, 4472, 318)])
        assert "cov-ex" in html_ and "78.3%" in html_

    def test_the_header_names_the_big_number(self):
        assert "Coverage excl. Partially" in self._html([("ICI", 5713, 4472, 318)])

    def test_the_colour_follows_the_ex_partial_figure(self):
        """ICI is amber on the real coverage and green without the gaps — the
        verdict must match the number it sits next to."""
        from src.ui.styles import coverage_health
        green = coverage_health(82.9)[1]
        assert green in self._html([("ICI", 5713, 4472, 318)])

    def test_one_line_only_when_they_agree(self):
        html_ = self._html([("Watsons Turkey", 1535, 1336, 0)])
        assert "cov-ex" not in html_ and "87.0%" in html_

    def test_missing_column_falls_back_to_the_real_coverage(self):
        """Older cached payloads and the Mobile App summary have no such column:
        the table must then behave exactly as it did before."""
        df = self._df([("ICI", 5713, 4472, 318)]).drop(
            columns=["Coverage excl. Partially %"])
        html_ = bl._summary_table_html(df, self._NUM)
        assert "cov-ex" not in html_ and "78.3%" in html_

    def _html(self, rows):
        return bl._summary_table_html(self._df(rows), self._NUM)


class TestUnknownReasons:
    """The Unknown export has to say WHY, or it is just the same number in a
    file.  Each reason is modelled on a case diagnosed against live TestRail
    data, and the strings are stable because they are the grouping key of the
    summary sheet — the whole point being that one fix clears a batch."""

    @staticmethod
    def _wire(monkeypatch, bu, case, country="NL", device="Desktop"):
        exp = pd.DataFrame([{"case_id": case["case_id"], "country_label": country,
                             "device": device, "category": "unknown"}])
        raw = pd.DataFrame([case])
        rules = [r for r in br.ALL_RULES if r.bu == bu and r.scope == "website"]
        monkeypatch.setattr(bl, "_backlog_data",
                            lambda: (pd.DataFrame(), {(bu, "website"): exp}, {}))
        monkeypatch.setattr(bl, "_load_scope",
                            lambda scope: (raw, pd.DataFrame(), rules))
        return dq._unknown_detail(bu, "website")

    def test_automated_in_a_field_the_bu_does_not_read(self, monkeypatch):
        """The Kruidvat case: "Automation Status" = Automated, but KV is decided
        by "Automation Status KV SPR"."""
        out = self._wire(monkeypatch, "Kruidvat", {
            "case_id": 4849997, "title": "Below MOV", "url": "u",
            "status_Automation Status": "Automated",
            "multi_countries": ["KVBE", "KVN"], "labels": ["big_regr_desktop"]})
        # KV *does* read "Automation Status", but only through the Playwright
        # rule, which is gated on the `playwright` label this case has not got.
        assert out.iloc[0]["Reason"] == dq._R_MISSING_LABEL
        assert "playwright" in out.iloc[0]["Evidence"]

    def test_automated_in_a_field_no_rule_reads(self, monkeypatch):
        """Trekpleister is decided by "Automation Status TP"; a value parked in
        the MFR field is read by nobody here."""
        out = self._wire(monkeypatch, "Trekpleister", {
            "case_id": 7, "title": "x", "url": "u",
            "status_Automation Status MFR": "Automated",
            "multi_countries": ["TP"], "labels": ["big_regr_desktop"]})
        assert out.iloc[0]["Reason"] == dq._R_WRONG_FIELD
        assert "Automation Status TP" in out.iloc[0]["Evidence"]

    def test_country_field_left_empty(self, monkeypatch):
        """The Watsons case: Testim automated, Testim Country Coverage empty."""
        out = self._wire(monkeypatch, "Watsons Turkey", {
            "case_id": 2708290, "title": "Delivery address", "url": "u",
            "status_Automation Status Testim Desktop": "Automated UAT",
            "multi_countries": ["WTR"], "testim_country_coverage": [],
            "labels": ["big_regr_desktop"]}, country="TR")
        assert out.iloc[0]["Reason"] == dq._R_COUNTRY_EMPTY
        assert "Testim Country Coverage" in out.iloc[0]["Evidence"]

    def test_country_field_covers_another_country(self, monkeypatch):
        """The Marionnaud case: automated and labelled java, but Java Country
        Coverage names CH while this row is France."""
        out = self._wire(monkeypatch, "Marionnaud", {
            "case_id": 4850869, "title": "eGiftcard", "url": "u",
            "status_Automation Status": "Automated",
            "java_country_coverage": ["MCH"],
            "multi_countries": ["MFR", "MCH"],
            "labels": ["big_regr_desktop", "java"]},
            country="FR")
        assert out.iloc[0]["Reason"] == dq._R_COUNTRY_MISS
        assert "MCH" in out.iloc[0]["Evidence"]

    def test_marionnaud_automated_without_its_framework_label(self, monkeypatch):
        """The migration's own failure mode: the status field is filled but the
        case never got a `java` or `playwright` label, so no rule claims it."""
        out = self._wire(monkeypatch, "Marionnaud", {
            "case_id": 4850870, "title": "Checkout", "url": "u",
            "status_Automation Status": "Automated",
            "java_country_coverage": ["MFR"],
            "multi_countries": ["MFR"], "labels": ["big_regr_desktop"]},
            country="FR")
        assert out.iloc[0]["Reason"] == dq._R_MISSING_LABEL

    def test_no_status_at_all(self, monkeypatch):
        out = self._wire(monkeypatch, "Kruidvat", {
            "case_id": 1, "title": "x", "url": "u",
            "multi_countries": ["KVN"], "labels": ["big_regr_desktop"]})
        assert out.iloc[0]["Reason"] == dq._R_NO_STATUS

    def test_evidence_columns_are_carried(self, monkeypatch):
        out = self._wire(monkeypatch, "Kruidvat", {
            "case_id": 1, "title": "x", "url": "https://tr/1",
            "status_Automation Status": "Automated",
            "multi_countries": ["KVN"], "labels": ["big_regr_desktop"]})
        for col in ("Case ID", "Country", "Device", "Reason", "Evidence",
                    "multi_countries", "Testim Country Coverage", "TestRail Link"):
            assert col in out.columns, col

    def test_summary_groups_and_ranks(self):
        detail = pd.DataFrame([{"Case ID": f"C{i}", "Reason": r}
                               for i, r in enumerate(["A"] * 7 + ["B"] * 3)])
        g = dq._unknown_reason_summary(detail)
        assert g.iloc[0]["Reason"] == "A" and g.iloc[0]["Rows"] == 7
        assert g.iloc[0]["Share of rows"] == "70.0%"

    def test_workbook_is_deferred_and_has_both_sheets(self, monkeypatch):
        import io

        import openpyxl
        calls = []
        monkeypatch.setattr(dq, "_unknown_detail",
                            lambda bu, sc: calls.append(1) or pd.DataFrame(
                                [{"Case ID": "C1", "Reason": "A"}]))
        build = dq._unknown_workbook("Kruidvat", "website")
        assert calls == []                      # nothing built until clicked
        wb = openpyxl.load_workbook(io.BytesIO(build()))
        assert wb.sheetnames == ["Reasons", "Rows"]


class TestProdSanityBaseline:
    """An independent baseline that may overlap the regression one: a case with
    both labels is counted in BOTH.  "100 automated, 5 of them prod sanity"
    reads 100 and 5 — the 5 are not taken out of the 100."""

    @staticmethod
    def _rule():
        return SimpleNamespace(
            bu="Drogas", scope="website", suite_id=1,
            countries_filter=["DRG LV"], country_labels={"DRG LV": "LV"},
            status_field_label="Automation Status",
            country_field_label="multi_countries")

    def test_regression_expansion_is_untouched(self):
        """The parameter defaults to the regression baseline — same rows, same
        order, byte for byte."""
        raw = pd.DataFrame([_case(case_id=1), _case(case_id=2)])
        a = bl._expand_baseline(raw, [self._rule()])
        b = bl._expand_baseline(raw, [self._rule()], member_label=None)
        assert a.equals(b)

    def test_membership_comes_from_the_new_label(self):
        raw = pd.DataFrame([
            _case(case_id=1, labels=["big_regr_desktop"]),
            _case(case_id=2, labels=["big_regr_desktop", "prod_sanity"]),
        ])
        out = bl._expand_baseline(raw, [self._rule()],
                                  member_label="prod_sanity")
        assert set(out["case_id"]) == {2}

    def test_a_case_in_both_expands_the_same_way_in_both(self):
        """What makes "5 of those 100" literally true rather than approximately."""
        raw = pd.DataFrame([_case(case_id=1,
                                  labels=["big_regr_desktop", "big_regr_mobile",
                                          "prod_sanity"])])
        regr = bl._expand_baseline(raw, [self._rule()])
        ps   = bl._expand_baseline(raw, [self._rule()], member_label="prod_sanity")
        assert regr[["country_label", "device"]].equals(
            ps[["country_label", "device"]])

    def test_a_prod_sanity_only_case_falls_back_to_the_device_field(self):
        """No big_regr label to read, so the TestRail Device field decides."""
        raw = pd.DataFrame([_case(case_id=1, labels=["prod_sanity"],
                                  device="Both")])
        out = bl._expand_baseline(raw, [self._rule()],
                                  member_label="prod_sanity")
        assert sorted(out["device"]) == ["Desktop", "Mobile"]

    def test_regression_ignores_a_prod_sanity_only_case(self):
        """The guarantee that nothing above can move: a case carrying only the
        new label must not appear in the regression baseline."""
        raw = pd.DataFrame([_case(case_id=1, labels=["prod_sanity"],
                                  device="Both")])
        assert bl._expand_baseline(raw, [self._rule()]).empty

    def test_the_run_reports_nothing_until_the_label_exists(self, monkeypatch):
        monkeypatch.setattr(bl, "_prod_sanity_data",
                            lambda: (pd.DataFrame(), {}, {}))
        summary, _e, _a = bl._run_data(bl.RUN_PS, "website")
        assert summary.empty


class TestProdSanityComesFromTheLabel:
    """"Test Automation PRD Run" no longer counts.  Changing the definition at
    the source rather than at each call site is what keeps the Coverage tab's
    Production Sanity view and the Backlog baseline on ONE definition."""

    @staticmethod
    def _reg():
        return SimpleNamespace(field=lambda lbl: None)

    def test_label_makes_it_prod_sanity(self, monkeypatch):
        monkeypatch.setattr(eng, "_get_labels", lambda c, pid: ["prod_sanity"])
        assert eng._get_prod_sanity({}, self._reg(), 1) is True

    def test_matched_case_insensitively(self, monkeypatch):
        monkeypatch.setattr(eng, "_get_labels", lambda c, pid: ["Prod_Sanity"])
        assert eng._get_prod_sanity({}, self._reg(), 1) is True

    def test_the_old_checkbox_no_longer_counts(self, monkeypatch):
        """A case with the field set but no label must NOT be prod sanity."""
        monkeypatch.setattr(eng, "_get_labels", lambda c, pid: ["big_regr_desktop"])
        assert eng._get_prod_sanity(
            {"custom_test_automation_prd_run": True}, self._reg(), 1) is False

    def test_one_spelling_shared_with_the_baseline(self):
        assert bl._LABEL_PROD_SANITY == br.PROD_SANITY_LABEL


class TestCoverageHeaderNamesItsFigure:
    """The column header must name the figure it leads with.  Production Sanity
    has no Partially category, so "excl. Partially" there would describe a
    distinction that is not on screen."""

    @staticmethod
    def _df(partial):
        return pd.DataFrame([{
            "BU": "ICI", "Scope": "Website", "Total": 100, "Automated": 80,
            "Backlog": 20 - partial, "Partially Automated": partial,
            "Coverage %": 80.0,
            "Coverage excl. Partially %": round(80 / (100 - partial) * 100, 1)}])

    def test_qualified_when_a_row_has_partial_gaps(self):
        assert "Coverage excl. Partially" in bl._summary_table_html(
            self._df(10), ["Total", "Automated"])

    def test_plain_when_none_has(self):
        html_ = bl._summary_table_html(self._df(0), ["Total", "Automated"])
        assert ">Coverage<" in html_ and "excl. Partially" not in html_


class TestBacklogHealthIsRegressionOnly:
    """The 3% health threshold was agreed for the regression baseline.  Borrowing
    it for Production Sanity would show a verdict nobody has agreed to."""

    @staticmethod
    def _df():
        return pd.DataFrame([{"BU": "ICI", "Scope": "Website", "Total": 100,
                              "Automated": 40, "Backlog": 60,
                              "Coverage %": 40.0}])

    def test_shown_by_default(self):
        """Still there after Backlog was folded into the stacked cell."""
        assert "bl-pct" in bl._summary_table_html(self._df(), ["Total", "Backlog"])

    def test_off_for_prod_sanity(self):
        html_ = bl._summary_table_html(self._df(), ["Total", "Backlog"],
                                       backlog_health=False)
        assert "bl-pct" not in html_ and ">60<" in html_


class TestLabelResolutionCost:
    """`_get_labels` runs once per CASE and used to reach through
    `@st.cache_data` every time — at ~24k cases the cache machinery, not the
    fetch, was the cost.  It showed up as a cold start that never painted."""

    def test_label_map_is_fetched_once_per_project(self, monkeypatch):
        calls = []
        eng._LABEL_MAP_MEMO.clear()
        monkeypatch.setattr(eng.tr, "fetch_labels",
                            lambda pid: calls.append(pid) or {1: "playwright"})
        for _ in range(500):
            eng._get_labels({"labels": [{"id": 1}]}, 7)
        assert calls == [7]

    def test_a_fresh_evaluation_re_reads_the_map(self, monkeypatch):
        """The memo must not outlive the cache entry it was built alongside."""
        eng._LABEL_MAP_MEMO[7] = {1: "stale"}
        monkeypatch.setattr(eng.tr, "fetch_labels", lambda pid: {1: "fresh"})
        eng._LABEL_MAP_MEMO.clear()          # what evaluate_rules() now does
        assert eng._get_labels({"labels": [{"id": 1}]}, 7) == ["fresh"]

    def test_prod_sanity_reuses_already_resolved_labels(self, monkeypatch):
        """Passing the list must not trigger a second resolution."""
        def _boom(*a, **k):
            raise AssertionError("resolved labels twice for one case")

        monkeypatch.setattr(eng, "_get_labels", _boom)
        assert eng._get_prod_sanity({}, None, labels=["prod_sanity"]) is True

    def test_no_label_means_no_pipeline(self):
        """An 11-BU expansion that can only produce empty frames is not worth
        running on every cold start."""
        raw = pd.DataFrame([{"labels": ["big_regr_desktop"]}])
        assert bl._carries_label(raw, "prod_sanity") is False
        assert bl._carries_label(
            pd.DataFrame([{"labels": ["prod_sanity"]}]), "prod_sanity") is True

class TestOutstandingStack:
    """Backlog · To be Updated · Partially answer one question — what is not
    covered by a working script — so they share one column.  Three headers wide
    enough to hold "PARTIALLY AUTOMATED" cost more width than the figures ever
    needed."""

    @staticmethod
    def _df():
        return pd.DataFrame([{
            "BU": "ICI", "Scope": "Website", "Total": 5713, "Automated": 4472,
            "Backlog": 237, "To be Updated": 376, "Partially Automated": 318,
            "Coverage %": 78.3}])

    _NUM = ["Total", "Automated", "Backlog", "Partially Automated",
            "To be Updated"]

    def test_one_header_replaces_three(self):
        html_ = bl._summary_table_html(self._df(), self._NUM)
        assert ">Outstanding<" in html_
        for gone in (">Backlog<", ">Partially Automated<", ">To be Updated<"):
            assert gone not in html_.split("<tbody>")[0], gone

    def test_all_three_figures_survive(self):
        html_ = bl._summary_table_html(self._df(), self._NUM)
        for n in ("237", "376", "318"):
            assert f"<b>{n}</b>" in html_, n

    def test_order_is_least_to_most_covered(self):
        html_ = bl._summary_table_html(self._df(), self._NUM)
        body = html_.split("<tbody>")[1]
        assert (body.index("BACKLOG".title()) < body.index("To be Updated")
                < body.index("Partially"))

    def test_the_health_verdict_keeps_its_place(self):
        """It used to hang off the Backlog column; folding that column in must
        not drop it."""
        assert "bl-pct" in bl._summary_table_html(self._df(), self._NUM)

    def test_header_and_body_cell_counts_agree(self):
        out = bl._summary_table_html(self._df(), self._NUM)
        head, body = out.split("<tbody>")
        n_th = len(re.findall(r"<th[ >]", head))
        for row in body.split("<tr")[1:]:
            assert row.count("<td") == n_th


class TestProdSanityAgreesAcrossTabs:
    """Production Sanity used to be case-based on the Coverage tab and row-based
    on the Backlog tab: one word, two numbers, two tabs.  Both now go through
    the same expansion and the same classification."""

    @staticmethod
    def _fixture(monkeypatch):
        rule = SimpleNamespace(
            bu="Drogas", scope="website", suite_id=1,
            countries_filter=["DRG LV", "DRG LT"],
            country_labels={"DRG LV": "LV", "DRG LT": "LT"},
            status_field_label="Automation Status",
            country_field_label="multi_countries")
        raw = pd.DataFrame([
            _case(case_id=1, labels=["big_regr_desktop", "prod_sanity"],
                  section_path="SD > Checkout", url="u1"),
            _case(case_id=2, labels=["big_regr_desktop"],
                  section_path="SD > Checkout", url="u2"),
            _case(case_id=3, labels=["prod_sanity"], device="Both",
                  section_path="SD > Content", url="u3"),
        ])
        auto = pd.DataFrame([{"case_id": 1, "country_label": "LV",
                              "device": "Desktop"}])
        monkeypatch.setattr(bl, "_load_scope", lambda scope: (raw, auto, [rule]))
        return raw, auto, [rule]

    def test_the_two_tabs_expand_the_same_rows(self, monkeypatch):
        raw, auto, rules = self._fixture(monkeypatch)
        backlog = bl._classify_expanded(
            bl._expand_baseline(raw, rules, member_label="prod_sanity"), auto)
        _nd, _ab, _ids, coverage = cov._baseline_like_backlog(
            raw, auto, rules, member_label="prod_sanity")
        assert len(backlog) == len(coverage)
        assert (sorted(backlog["case_id"].astype(int))
                == sorted(coverage["case_id"].astype(int)))

    def test_it_is_rows_not_cases(self, monkeypatch):
        """Case 3 carries only the prod_sanity label with Device=Both, so it
        contributes more rows than cases — which is the whole difference."""
        raw, auto, rules = self._fixture(monkeypatch)
        exp = bl._expand_baseline(raw, rules, member_label="prod_sanity")
        assert len(exp) > exp["case_id"].nunique()

    def test_the_regression_view_is_untouched(self, monkeypatch):
        raw, auto, rules = self._fixture(monkeypatch)
        _nd, _ab, _ids, regr = cov._baseline_like_backlog(raw, auto, rules)
        assert set(regr["case_id"].astype(int)) == {1, 2}


class TestOutstandingTracksAreFixed:
    """The verdict used to share the label's grid cell, so it widened that
    column on the Backlog line only — and with the block anchored right, any row
    with a wider figure or verdict slid sideways.  Three separate cells, so the
    markup a browser lays out is identical in shape on every row."""

    @staticmethod
    def _row(backlog, partial, tbu, total):
        return pd.DataFrame([{
            "BU": "X", "Scope": "Website", "Total": total, "Automated": 1,
            "Backlog": backlog, "Partially Automated": partial,
            "To be Updated": tbu, "Coverage %": 50.0}])

    _NUM = ["Total", "Backlog", "Partially Automated", "To be Updated"]

    def test_every_line_has_the_same_three_cells(self):
        html_ = bl._summary_table_html(self._row(19, 0, 21, 1260), self._NUM)
        cell = html_[html_.index('<span class="stack">'):html_.index("</span></td>")]
        assert cell.count("<u>") == 3 and cell.count("<i>") == 3
        assert cell.count("<b>") == 3

    def test_the_verdict_never_shares_the_label_cell(self):
        html_ = bl._summary_table_html(self._row(19, 0, 21, 1260), self._NUM)
        assert "bl-pct" in html_
        # the verdict closes before the label opens
        assert "</u><i>" in html_

    def test_shape_is_identical_with_a_four_figure_number(self):
        """Marionnaud's 1,419 must not reshape the block.  Compares the TAG
        sequence only: the verdict's colour and tooltip legitimately differ
        between a healthy backlog and an unhealthy one."""
        def _tags(df):
            h = bl._summary_table_html(df, self._NUM)
            cell = h[h.index('class="stack"'):h.index("</span></td>")]
            return re.findall(r"</?([a-z]+)", cell)

        assert (_tags(self._row(19, 0, 21, 1260))
                == _tags(self._row(240, 1419, 130, 5778)))


class TestLabelMemoSurvivesCacheHits:
    """`evaluate_rules` is the single-flight WRAPPER — it runs on every call,
    hits included.  Clearing the memo there wiped it on every render and sent
    `_get_labels` back through `@st.cache_data` once per case, which is the cost
    the memo exists to remove."""

    def test_the_wrapper_does_not_clear_the_memo(self, monkeypatch):
        eng._LABEL_MAP_MEMO.clear()
        eng._LABEL_MAP_MEMO[7] = {1: "playwright"}
        monkeypatch.setattr(eng, "_evaluate_rules_cached", lambda names: "ok")
        eng.evaluate_rules(("KV JAVA",))
        assert eng._LABEL_MAP_MEMO == {7: {1: "playwright"}}

    def test_a_real_recomputation_does_clear_it(self, monkeypatch):
        """The memo must not outlive the cache entry it was built alongside."""
        import inspect
        src = inspect.getsource(eng._evaluate_rules_cached)
        assert "_LABEL_MAP_MEMO.clear()" in src


class TestFilterRecipe:
    """The download carries a second sheet saying how to pull the same subset
    out of TestRail.  It is generated from the rules, so it cannot drift from
    the numbers it explains — and it names what TestRail cannot express, which
    is what a mismatch actually needs explaining."""

    def test_names_the_field_this_bu_is_decided_by(self):
        r = bl._filter_recipe("Kruidvat", "website", "backlog", 90, 54)
        fields = " ".join(r["Field"])
        assert "Automation Status KV SPR" in fields
        assert "Automation Status Testim Desktop" in fields

    def test_the_playwright_rule_carries_its_label_gate(self):
        """Filtering on the status alone would pull in every legacy case that
        shares the generic field."""
        r = bl._filter_recipe("Kruidvat", "website", "automated", 10, 5)
        row = r[r["Field"].str.contains("playwright")].iloc[0]
        assert "AND label: playwright" in row["Filter"]

    def test_the_predicate_matches_the_category(self):
        auto = bl._filter_recipe("Drogas", "website", "automated", 1, 1)
        na   = bl._filter_recipe("Drogas", "website", "not_applicable", 1, 1)
        assert "one of: Automated" in " ".join(auto["Filter"])
        assert "Automation not applicable" in " ".join(na["Filter"])

    def test_it_states_what_testrail_cannot_do(self):
        for cat, needle in (("backlog", "automated NOWHERE"),
                            ("partially_automated", "No TestRail filter"),
                            ("automated", "wins over Automated")):
            r = bl._filter_recipe("ICI Paris XL", "website", cat, 1, 1)
            assert needle in " ".join(r["Filter"]), cat

    def test_it_states_both_counts(self):
        r = bl._filter_recipe("Drogas", "website", "backlog", 90, 54)
        assert "90 rows over 54 cases" in " ".join(r["Filter"])

    def test_the_conditional_country_is_called_out(self):
        r = bl._filter_recipe("ICI Paris XL", "website", "total", 1, 1)
        assert "Highest" in " ".join(r["Filter"])

    def test_prod_sanity_names_its_own_label(self):
        r = bl._filter_recipe("Drogas", "website", "total", 1, 1,
                              member_label="prod_sanity")
        assert "prod_sanity" in " ".join(r["Filter"])


class TestOneBuCannotDecideAnother:
    """TestRail custom fields are global, so a Perfume Shop case can carry a
    value in `Automation Status SD`.  Scanning every `status_*` column let
    Superdrug's field classify a TPS row — and the export honestly named it,
    which is how the QA lead found it."""

    @staticmethod
    def _rule(bu="The Perfume Shop", field="Automation Status"):
        return SimpleNamespace(
            bu=bu, scope="website", suite_id=1,
            status_field_label=field,
            countries_filter=["TPS GB"], country_labels={"TPS GB": "GB"},
            country_field_label="multi_countries")

    @staticmethod
    def _raw(**status):
        return pd.DataFrame([{
            "case_id": 1, "labels": ["big_regr_desktop"],
            "multi_countries": ["TPS GB"], "device": "Desktop",
            "type_label": "Regression", "priority_label": "High",
            "title": "t", "url": "u", "section_path": "TPS > X", **status}])

    def test_another_bus_field_no_longer_classifies(self):
        exp = bl._expand_baseline(
            self._raw(**{"status_Automation Status": None,
                         "status_Automation Status SD": "Automation not applicable"}),
            [self._rule()])
        assert exp.iloc[0]["_cat_base"] == "unknown"

    def test_the_bus_own_field_still_does(self):
        exp = bl._expand_baseline(
            self._raw(**{"status_Automation Status": "Automation not applicable"}),
            [self._rule()])
        assert exp.iloc[0]["_cat_base"] == "not_applicable"

    def test_the_device_path_cannot_smuggle_one_back(self):
        """The per-device TestIM re-classification must respect the same list."""
        exp = bl._expand_baseline(
            self._raw(**{"status_Automation Status": "Not automated",
                         "status_Automation Status Testim Desktop":
                             "Automation not applicable"}),
            [self._rule()])          # this rule does NOT read the Testim field
        assert exp.iloc[0]["_cat_base"] == "backlog"

    def test_the_export_cannot_name_a_field_that_did_not_decide(self):
        case = {"status_Automation Status": None,
                "status_Automation Status SD": "Automation not applicable"}
        allowed = ["status_Automation Status"]
        assert bl._deciding_field(case, "Desktop", "not_applicable", allowed)[0] == "—"
        # unrestricted, it still reports whatever it finds — the old behaviour
        assert bl._deciding_field(case, "Desktop", "not_applicable")[0] \
            == "Automation Status SD"


class TestSmallNrIsASubset:
    """Small NR marks a SUBSET of the big_regr baseline, not a baseline of its
    own: every case carrying it is already counted, so the filter can only
    narrow the rows — never add one."""

    @staticmethod
    def _expanded():
        return pd.DataFrame([
            {"case_id": 1, "country_label": "NL", "device": "Desktop",
             "category": "automated"},
            {"case_id": 2, "country_label": "NL", "device": "Desktop",
             "category": "backlog"},
            {"case_id": 3, "country_label": "NL", "device": "Desktop",
             "category": "automated"},
        ])

    def test_the_subset_never_grows_the_baseline(self, monkeypatch):
        exp = self._expanded()
        raw = pd.DataFrame([{"case_id": i, "small_nr": i in (1, 2)}
                            for i in (1, 2, 3)])
        monkeypatch.setattr(bl, "_load_scope",
                            lambda scope: (raw, pd.DataFrame(), []))
        monkeypatch.setattr(bl, "_backlog_data",
                            lambda: (pd.DataFrame([{"BU": "Drogas",
                                                    "Scope": "Website"}]),
                                     {("Drogas", "website"): exp}, {}))
        _summary, small, _auto = bl._run_data(bl.RUN_SMALL, "website")
        rows = small[("Drogas", "website")]
        assert len(rows) == 2 and set(rows["case_id"]) == {1, 2}
        assert len(rows) <= len(exp)

    def test_no_checkbox_means_an_empty_run(self, monkeypatch):
        monkeypatch.setattr(bl, "_load_scope",
                            lambda scope: (pd.DataFrame(), pd.DataFrame(), []))
        monkeypatch.setattr(bl, "_backlog_data",
                            lambda: (pd.DataFrame(), {}, {}))
        summary, _e, _a = bl._run_data(bl.RUN_SMALL, "website")
        assert summary.empty

    def test_the_big_run_is_the_untouched_payload(self, monkeypatch):
        payload = (pd.DataFrame([{"BU": "X"}]), {"k": "v"}, {"k": "w"})
        monkeypatch.setattr(bl, "_backlog_data", lambda: payload)
        assert bl._run_data(bl.RUN_BIG, "website") == payload


class TestRunsShareNoWidgetState:
    """Each run drives the same tiles and the same pivot, so their widget keys
    must differ: sharing one would let a Country/Device choice made on the big
    baseline reappear on Production Sanity, over different rows."""

    def test_the_pivot_key_carries_the_run(self):
        import inspect
        src = inspect.getsource(bl._detail_view)
        assert "run_key" in src and "_baseline_pivot" in src
        assert "{run_key}" in src

    def test_the_tile_keys_carry_the_run(self):
        import inspect
        src = inspect.getsource(bl._detail_view)
        head = src[:src.index("_baseline_pivot")]
        assert "RUNS.index(run)" in head

    def test_prod_sanity_tiles_export_prod_sanity_rows(self):
        import inspect
        src = inspect.getsource(bl._detail_view)
        assert 'baseline=("prod_sanity" if run == RUN_PS' in src


class TestSmallNrFieldResolution:
    """The checkbox is known by its LABEL in the TestRail UI and by a system
    name in the API.  Narrowing the lookup to one of them emptied the whole run
    without an error — the column simply never appeared."""

    @staticmethod
    def _reg(known):
        return SimpleNamespace(field=lambda n: (
            SimpleNamespace(system_name="custom_x") if n == known else None))

    @pytest.mark.parametrize("known", ["Smaller NR", "small_nr",
                                       "custom_small_nr", "custom_smaller_nr"])
    def test_resolves_under_any_of_its_names(self, known):
        assert eng._get_small_nr({"custom_x": True}, self._reg(known)) is True

    def test_unticked_is_false_not_missing(self):
        assert eng._get_small_nr({"custom_x": False},
                                 self._reg("small_nr")) is False

    def test_an_unknown_field_does_not_raise(self):
        assert eng._get_small_nr({}, self._reg("something else")) is False


class TestRunMeanings:
    """Switching run changes every number on the page, so each one carries the
    sentence that says what it is — the explanation that had nowhere to live
    once the tile tooltips went."""

    def test_every_run_has_one(self):
        assert set(bl._RUN_MEANING) == set(bl.RUNS)
        assert all(v.strip() for v in bl._RUN_MEANING.values())

    def test_each_names_the_field_it_is_defined_by(self):
        assert "big_regr" in bl._RUN_MEANING[bl.RUN_BIG]
        assert "small_nr" in bl._RUN_MEANING[bl.RUN_SMALL]
        assert "prod_sanity" in bl._RUN_MEANING[bl.RUN_PS]

    def test_the_subset_is_described_as_one(self):
        """Small NR narrows the baseline; calling it anything else would invite
        adding its total to the big one."""
        assert "subset" in bl._RUN_MEANING[bl.RUN_SMALL].lower()

    def test_prod_sanity_says_it_is_separate(self):
        assert "separately" in bl._RUN_MEANING[bl.RUN_PS]


class TestCountryColumnBelongsToItsBu:
    """Country fields carry the tokens of every BU sharing the suite, so an ICI
    row listed "IPXL NL, MRN, MFR" and read as if it counted for Marionnaud.
    The counts never did — a case with no token of this BU is dropped before
    expansion — but a column saying otherwise is worse than no column."""

    @staticmethod
    def _rule():
        return SimpleNamespace(
            bu="ICI Paris XL", scope="website", suite_id=1, framework="java",
            status_field_label="Automation Status", automated_values=["Automated"],
            countries_filter=["IPXL NL", "IPXL BE"],
            country_labels={"IPXL NL": "NL", "IPXL BE": "BE"},
            country_field_label="multi_countries", labels_filter=[],
            type_filter=[])

    @staticmethod
    def _raw(countries):
        return pd.DataFrame([{
            "case_id": 1, "title": "t", "url": "u", "section_path": "ICI > X",
            "labels": ["big_regr_desktop"], "multi_countries": countries,
            "device": "Desktop", "type_label": "Regression",
            "priority_label": "High",
            "status_Automation Status": "Not automated"}])

    def _evidence(self, monkeypatch, countries):
        rule = self._rule()
        raw = self._raw(countries)
        monkeypatch.setattr(bl, "_load_scope", lambda scope: (raw, pd.DataFrame(), [rule]))
        monkeypatch.setattr(bl, "ALL_RULES", [rule])
        exp = bl._classify_expanded(bl._expand_baseline(raw, [rule]), pd.DataFrame())
        return bl._evidence_frame(exp, "website", "ICI Paris XL")

    def test_only_this_bus_tokens_are_shown_as_counted(self, monkeypatch):
        ev = self._evidence(monkeypatch, ["IPXL NL", "MRN", "MFR"])
        assert ev.iloc[0]["Countries counted for this BU"] == "IPXL NL"

    def test_the_others_are_kept_but_named_as_others(self, monkeypatch):
        """Still visible — they explain the TestRail field — but never mistaken
        for something this BU is counted on."""
        ev = self._evidence(monkeypatch, ["IPXL NL", "MRN", "MFR"])
        assert ev.iloc[0]["Other BUs on this case"] == "MRN, MFR"

    def test_no_extra_column_when_the_case_is_this_bus_only(self, monkeypatch):
        ev = self._evidence(monkeypatch, ["IPXL NL", "IPXL BE"])
        assert "Other BUs on this case" not in ev.columns

    def test_a_case_with_no_token_of_this_bu_produces_no_rows(self, monkeypatch):
        """The counting guarantee behind all of the above."""
        rule = self._rule()
        assert bl._expand_baseline(self._raw(["MRN"]), [rule]).empty


class TestRateLimitPolicy:
    """The cap MOVES — the 429 body has said 180, then 50 (2026-08-14), then 5
    (2026-08-17) requests/minute — and each time we were still pacing for the
    old number the dashboard stopped loading entirely.  So the number is no
    longer ours to maintain: we start pessimistic and believe the limiter.

    A 429 must also be waited out rather than given up on: TestRail asks for
    41-57s when the window is full, and the old 30s cap failed the load before
    it reopened."""

    def test_the_pacer_stays_under_the_cap(self):
        """The invariant, at any pool size: one account never exceeds its own
        allowance, whatever that allowance currently is."""
        from src import testrail_client as trc
        trc._repace()
        assert (60 / trc._PACE_INTERVAL) / trc._pool_size <= trc._effective_limit()

    def test_the_default_is_the_pessimistic_one(self):
        """Guessing low costs minutes; guessing high costs the dashboard."""
        from src import testrail_client as trc
        assert trc._DEFAULT_LIMIT <= 5

    def test_the_cap_is_read_out_of_the_429_body(self, monkeypatch):
        """TestRail states the real number in the error it sends back."""
        from src import testrail_client as trc
        monkeypatch.setattr(trc, "_limit_observed", None)
        monkeypatch.setattr(trc, "_limit_declared", 50)
        trc._learn_limit('{"error":"API Rate Limit Exceeded - 5 per minute '
                         'maximum allowed. Retry after 57 seconds."}')
        assert trc._effective_limit() == 5

    def test_the_cap_is_never_raised_by_a_429(self, monkeypatch):
        """A stray higher figure must not undo what we already learned — the
        expensive mistake is always the optimistic one."""
        from src import testrail_client as trc
        monkeypatch.setattr(trc, "_limit_observed", 5)
        monkeypatch.setattr(trc, "_limit_declared", 50)
        trc._learn_limit("API Rate Limit Exceeded - 200 per minute maximum allowed.")
        assert trc._effective_limit() == 5

    def test_a_429_backs_the_WHOLE_pacer_off(self, monkeypatch):
        """The avalanche: the thread that got the 429 always waited politely,
        while the other seventy-nine fired into the same closed window."""
        from src import testrail_client as trc
        monkeypatch.setattr(trc.time, "sleep", lambda s: None)
        monkeypatch.setattr(trc, "_PACE_NEXT", 0.0)
        trc._pace_cooldown(44)
        assert trc._PACE_NEXT >= trc.time.time() + 43

    def test_a_429_is_retried_until_the_window_reopens(self, monkeypatch):
        from src import testrail_client as trc
        sleeps: list[float] = []
        monkeypatch.setattr(trc.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(trc, "_pace", lambda: None)

        class _Resp:
            def __init__(self, code):
                self.status_code, self.ok = code, code == 200
                self.headers = {"Retry-After": "44"}
                self.text = "{}"

            def json(self):
                return {"ok": True}

        seq = [_Resp(429), _Resp(429), _Resp(200)]
        import itertools
        client = trc.TestRailClient.__new__(trc.TestRailClient)
        client._sessions = [SimpleNamespace(get=lambda *a, **k: seq.pop(0))]
        client._rr = itertools.count()
        client.timeout = 5
        client._url = lambda e: e
        client.creds = SimpleNamespace(base_url="https://x")
        assert client._get("get_cases/1") == {"ok": True}
        # waited the full Retry-After each time, not a clipped 30s
        assert sleeps == [44, 44]

    def test_the_wait_is_still_capped(self, monkeypatch):
        """A hostile or buggy header must not stall a worker thread."""
        from src import testrail_client as trc
        sleeps: list[float] = []
        monkeypatch.setattr(trc.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(trc, "_pace", lambda: None)

        class _Resp:
            def __init__(self, code):
                self.status_code, self.ok = code, code == 200
                self.headers = {"Retry-After": "99999"}
                self.text = "{}"

            def json(self):
                return {}

        seq = [_Resp(429), _Resp(200)]
        import itertools
        client = trc.TestRailClient.__new__(trc.TestRailClient)
        client._sessions = [SimpleNamespace(get=lambda *a, **k: seq.pop(0))]
        client._rr = itertools.count()
        client.timeout = 5
        client._url = lambda e: e
        client._get("get_cases/1")
        assert sleeps == [60]


class TestCasePaginationAsksOnlyForWhatExists:
    """`get_cases` is the hot path — every number on the dashboard comes out of
    it — and it used to speculate five pages ahead, awaiting the whole batch
    before noticing the short page.  That was free while requests were cheap;
    against a 5/min cap each overshoot is a real ~3.5s.  Both halves are pinned
    here: every case still comes back, and nothing past the last page is ever
    requested."""

    def _client(self, pages):
        """A client whose `_get` replays *pages* and records what was asked."""
        from src import testrail_client as trc
        client = trc.TestRailClient.__new__(trc.TestRailClient)
        asked: list[str] = []

        def _get(endpoint):
            asked.append(endpoint)
            return pages[len(asked) - 1]

        client._get = _get
        return client, asked

    @staticmethod
    def _page(ids, nxt=None):
        return {"cases": [{"id": i} for i in ids], "_links": {"next": nxt}}

    def test_every_page_is_collected_in_order(self):
        nxt = "/api/v2/get_cases/1&suite_id=2&limit=250&offset={}"
        client, asked = self._client([
            self._page([1, 2], nxt.format(250)),
            self._page([3, 4], nxt.format(500)),
            self._page([5], None),
        ])
        assert [c["id"] for c in client.get_cases(1, 2)] == [1, 2, 3, 4, 5]
        assert len(asked) == 3

    def test_a_single_page_suite_costs_exactly_one_request(self):
        client, asked = self._client([self._page([1, 2, 3], None)])
        assert len(client.get_cases(1, 2)) == 3
        assert len(asked) == 1

    def test_nothing_is_requested_past_the_last_page(self):
        """The overshoot this replaces: a four-page suite used to cost six."""
        nxt = "get_cases/1&suite_id=2&limit=250&offset={}"
        client, asked = self._client([
            self._page(range(0, 250), nxt.format(250)),
            self._page(range(250, 500), nxt.format(500)),
            self._page(range(500, 750), nxt.format(750)),
            self._page(range(750, 900), None),
        ])
        assert len(client.get_cases(1, 2)) == 900
        assert len(asked) == 4

    def test_the_legacy_bare_list_shape_still_works(self):
        """Older TestRail deployments answer without a pagination envelope."""
        client, _ = self._client([[{"id": 1}, {"id": 2}]])
        assert [c["id"] for c in client.get_cases(1, 2)] == [1, 2]

    def test_a_next_link_that_returns_nothing_cannot_loop(self):
        """A self-referential `next` would otherwise never return — and at
        ~3.5s a request that is a warm-up that hangs instead of finishing."""
        forever = "get_cases/1&suite_id=2&limit=250&offset=250"
        client, asked = self._client(
            [self._page([1], forever)] + [self._page([], forever)] * 5)
        assert [c["id"] for c in client.get_cases(1, 2)] == [1]
        assert len(asked) == 2


class TestClosedHistoryIsNotRedownloaded:
    """A completed run or plan cannot change, so re-fetching it on a ten-minute
    TTL spent the rate limit on history that was already final — roughly 25
    requests per BU, taken out of the same budget the coverage data needs."""

    def test_completed_runs_route_to_the_persisted_cache(self, monkeypatch):
        from src import testrail_client as trc
        seen: list[tuple] = []
        monkeypatch.setattr(trc, "_fetch_runs_closed",
                            lambda pid: seen.append(("closed", pid)) or [])
        monkeypatch.setattr(trc, "_fetch_runs_live",
                            lambda pid, c: seen.append(("live", pid, c)) or [])
        trc.fetch_runs(1, is_completed=True)
        trc.fetch_runs(1, is_completed=False)
        trc.fetch_runs(1)
        assert seen == [("closed", 1), ("live", 1, False), ("live", 1, None)]

    def test_completed_plans_route_to_the_persisted_cache(self, monkeypatch):
        from src import testrail_client as trc
        seen: list[tuple] = []
        monkeypatch.setattr(trc, "_fetch_plans_closed",
                            lambda pid: seen.append(("closed", pid)) or [])
        monkeypatch.setattr(trc, "_fetch_plans_live",
                            lambda pid, c: seen.append(("live", pid, c)) or [])
        trc.fetch_plans(2, is_completed=True)
        trc.fetch_plans(2, is_completed=False)
        assert seen == [("closed", 2), ("live", 2, False)]

    def test_the_completed_plan_details_use_the_closed_fetcher(self):
        """The ~20 detail calls per BU are the bulk of the cost."""
        import inspect
        from src.ui import runs_tab
        src = inspect.getsource(runs_tab._completed_runs_for_bu)
        assert "fetch_plan_closed" in src
        assert "submit(tr.fetch_plan," not in src

    def test_the_active_plan_details_stay_on_the_short_ttl(self):
        """Active plans are exactly the thing that IS still moving."""
        import inspect
        from src.ui import runs_tab
        src = inspect.getsource(runs_tab._flatten_active_runs)
        assert "submit(tr.fetch_plan," in src
        assert "fetch_plan_closed" not in src

    def test_clearing_every_cache_still_resolves(self):
        """`clear_all_caches` names its functions by hand — a renamed cache
        turns the refresh button into a NameError at the worst moment."""
        from src import testrail_client as trc
        trc.clear_all_caches()


class TestPrewarmSurvivesABadWindow:
    """A warm-up that failed used to claim the next six hours: `_WARMED_AT` was
    set upfront and never reset, so one rate-limit storm left every later
    session fetching lazily and serially long after TestRail had recovered.
    The progress hook has to keep reporting throughout, too — at the current cap
    a single suite can take a minute, and a box that stands still that long is
    indistinguishable from a dead one."""

    def _stub(self, monkeypatch, trc, cases):
        monkeypatch.setattr(trc, "resolve_project_id", lambda sid: 1)
        monkeypatch.setattr(trc, "fetch_cases", cases)
        monkeypatch.setattr(trc, "fetch_sections", lambda p, s: [])
        monkeypatch.setattr(trc, "fetch_labels", lambda p: {})
        monkeypatch.setattr(trc, "_WARMED_AT", 0.0)

    def test_a_failed_warmup_lets_the_next_one_retry_soon(self, monkeypatch):
        from src import testrail_client as trc

        def _boom(pid, sid):
            raise trc.TestRailError("429: rate limited")

        self._stub(monkeypatch, trc, _boom)
        trc.prefetch_all_suites([1, 2])
        held = trc._WARM_INTERVAL - (trc.time.time() - trc._WARMED_AT)
        assert 0 < held <= trc._WARM_RETRY_AFTER + 5

    def test_a_clean_warmup_holds_the_full_interval(self, monkeypatch):
        from src import testrail_client as trc
        self._stub(monkeypatch, trc, lambda p, s: [])
        trc.prefetch_all_suites([1, 2])
        held = trc._WARM_INTERVAL - (trc.time.time() - trc._WARMED_AT)
        assert held > trc._WARM_RETRY_AFTER

    def test_progress_keeps_reporting_while_a_download_runs(self, monkeypatch):
        import threading
        from src import testrail_client as trc
        monkeypatch.setattr(trc, "_PROGRESS_TICK", 0.05)
        blocked = threading.Event()

        def _slow(pid, sid):
            blocked.wait(0.4)
            return []

        self._stub(monkeypatch, trc, _slow)
        seen: list[tuple] = []
        trc.prefetch_all_suites([1], on_progress=lambda d, t, r: seen.append((d, t, r)))
        assert len(seen) >= 3           # ticked DURING the download, not only after
        assert seen[-1][0] == seen[-1][1]   # and ended on a complete count

    def test_a_dead_ui_callback_cannot_abort_the_download(self, monkeypatch):
        """Other sessions are waiting on this same download."""
        from src import testrail_client as trc
        self._stub(monkeypatch, trc, lambda p, s: [])

        def _dead(*_a):
            raise KeyboardInterrupt("session gone")

        trc.prefetch_all_suites([1], on_progress=_dead)   # must not raise

    def test_successful_requests_are_counted(self, monkeypatch):
        """The counter the loader uses as its heartbeat."""
        import itertools
        from src import testrail_client as trc
        monkeypatch.setattr(trc, "_pace", lambda: None)

        class _Resp:
            status_code, ok, text, headers = 200, True, "{}", {}

            def json(self):
                return {"ok": 1}

        client = trc.TestRailClient.__new__(trc.TestRailClient)
        client._sessions = [SimpleNamespace(get=lambda *a, **k: _Resp())]
        client._rr = itertools.count()
        client.timeout = 5
        client._url = lambda e: e
        before = trc.requests_served()
        client._get("a")
        client._get("b")
        assert trc.requests_served() == before + 2


class TestDexterKnowsTheRuns:
    """Dexter's snapshot has to carry the same three runs the tab shows, on the
    same basis.  Production Sanity counted CASES here while the dashboard
    counted ROWS — the identical drift that once had Dexter quoting 91.9% where
    the screen said 95.2%."""

    def test_prod_sanity_is_row_based(self):
        import inspect
        from src.ui import chat_assistant as ca
        src = inspect.getsource(ca.get_bu_coverage)
        assert "_regression_stats(exp_ps)" in src
        assert 'nunique())' not in src.split("prod_sanity")[1][:400]

    def test_small_nr_is_filtered_not_re_expanded(self):
        import inspect
        from src.ui import chat_assistant as ca
        src = inspect.getsource(ca.get_bu_coverage)
        assert "_small_nr_cases(scope)" in src
        assert "isin(ids_small)" in src

    def test_both_runs_reach_the_brief(self):
        import inspect
        from src.ui import chat_assistant as ca
        src = inspect.getsource(ca)
        assert '"small_no_regression":' in src
        assert "Small No-Regression run" in src
        assert "Production Sanity run" in src

    def test_the_brief_says_the_runs_do_not_add_up(self):
        """A subset and a separate baseline summed together is the one arithmetic
        error the snapshot invites if it stays silent."""
        import inspect
        from src.ui import chat_assistant as ca
        src = inspect.getsource(ca)
        assert "Never sum two runs" in src
        assert "already counted in it" in src


class TestMultiAccountPool:
    """TestRail rate-limits per USER, so each working account raises the ceiling
    by 50/min.  The pool is DISCOVERED from secrets, never configured: adding
    the next account has to be a secrets edit and nothing else."""

    @staticmethod
    def _secrets(monkeypatch, mapping):
        from src import testrail_client as trc
        monkeypatch.setattr(trc.st, "secrets", mapping)

    def test_finds_every_account_present(self, monkeypatch):
        from src import testrail_client as trc
        self._secrets(monkeypatch, {
            "TESTRAIL_URL": "https://x", "TESTRAIL_USER": "a@x", "TESTRAIL_API_KEY": "k",
            "TESTRAIL_USER_1": "b@x", "TESTRAIL_API_KEY_1": "k1",
            "TESTRAIL_USER_2": "c@x", "TESTRAIL_API_KEY_2": "k2",
        })
        assert [c.user for c in trc._credential_sets()] == ["a@x", "b@x", "c@x"]

    def test_a_gap_does_not_hide_a_later_account(self, monkeypatch):
        """The accounts arrive one at a time; a half-filled _2 must not stop the
        search before _3."""
        from src import testrail_client as trc
        self._secrets(monkeypatch, {
            "TESTRAIL_URL": "https://x", "TESTRAIL_USER": "a@x", "TESTRAIL_API_KEY": "k",
            "TESTRAIL_USER_1": "b@x", "TESTRAIL_API_KEY_1": "k1",
            "TESTRAIL_USER_3": "d@x", "TESTRAIL_API_KEY_3": "k3",
        })
        assert [c.user for c in trc._credential_sets()] == ["a@x", "b@x", "d@x"]

    def test_blank_credentials_are_ignored(self, monkeypatch):
        from src import testrail_client as trc
        self._secrets(monkeypatch, {
            "TESTRAIL_URL": "https://x", "TESTRAIL_USER": "a@x", "TESTRAIL_API_KEY": "k",
            "TESTRAIL_USER_1": "  ", "TESTRAIL_API_KEY_1": "",
        })
        assert [c.user for c in trc._credential_sets()] == ["a@x"]

    def test_a_duplicated_user_is_dropped(self, monkeypatch):
        """Two entries for one user share ONE rate-limit budget while we would
        pace as if there were two — faster on paper, 429s in practice."""
        from src import testrail_client as trc
        self._secrets(monkeypatch, {
            "TESTRAIL_URL": "https://x", "TESTRAIL_USER": "a@x", "TESTRAIL_API_KEY": "k",
            "TESTRAIL_USER_1": "A@X", "TESTRAIL_API_KEY_1": "k1",
        })
        assert [c.user for c in trc._credential_sets()] == ["a@x"]

    def test_requests_alternate_across_the_accounts(self):
        import itertools
        from src import testrail_client as trc
        client = trc.TestRailClient.__new__(trc.TestRailClient)
        client._sessions = ["s0", "s1", "s2"]
        client._rr = itertools.count()
        assert [client._next_session() for _ in range(6)] == \
            ["s0", "s1", "s2", "s0", "s1", "s2"]

    def test_the_pacer_keeps_each_account_under_its_own_cap(self):
        """Whatever the pool size, one account must never exceed its own cap.

        Round-robin is what makes this hold: consecutive slots land on
        consecutive accounts, so the aggregate rate divides cleanly by the
        pool size instead of piling onto one user."""
        from src import testrail_client as trc
        cap = trc._effective_limit()
        for n in (1, 2, 3, 5, 8):
            interval = 60.0 / (cap * n * trc._HEADROOM)
            per_account = (60 / interval) / n
            assert per_account <= cap, n


class TestWorkerPoolIsBuiltOnce:
    """The warm-up submits up to 8 suite workers at once and they all reach the
    pool builder before it exists.  Unguarded, each probed EVERY account — on a
    pool of three that is 24 wasted requests, paced at the single-account rate
    because the pacer had not widened yet, which is most of the speed-up spent
    before any real work starts."""

    def test_concurrent_callers_build_one_pool(self, monkeypatch):
        import threading
        from src import testrail_client as trc

        builds: list[int] = []
        trc._SESSION_CACHE.clear()
        creds = [trc.TestRailCredentials("https://x", f"u{i}@x", "k")
                 for i in range(3)]
        monkeypatch.setattr(trc, "_credential_sets",
                            lambda: builds.append(1) or creds)
        monkeypatch.setattr(trc, "_account_works", lambda c: True)
        monkeypatch.setattr(trc.TestRailClient, "_make_session",
                            lambda self, c: object())

        threads = [threading.Thread(target=trc._get_client) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert builds == [1]
        assert trc._SESSION_CACHE["pool"].n_accounts == 3
        trc._SESSION_CACHE.clear()

    def test_n_workers_is_zero_before_the_pool_exists(self):
        from src import testrail_client as trc
        trc._SESSION_CACHE.clear()
        assert trc.n_workers() == 0


class TestRejectedAccountsAreVisible:
    """"3 workers" when four are configured is the difference between a slow day
    and a silently rejected account.  The counts must survive the pool build so
    the UI can say "3 of 4" instead of just "3"."""

    def test_both_counts_are_recorded(self, monkeypatch):
        from src import testrail_client as trc
        trc._SESSION_CACHE.clear()
        creds = [trc.TestRailCredentials("https://x", f"u{i}@x", "k")
                 for i in range(4)]
        monkeypatch.setattr(trc, "_credential_sets", lambda: creds)
        monkeypatch.setattr(trc, "_account_works",
                            lambda c: c.user != "u2@x")     # one rejected
        monkeypatch.setattr(trc.TestRailClient, "_make_session",
                            lambda self, c: object())
        trc._get_client()
        assert trc.n_workers() == 3
        assert trc.n_accounts_configured() == 4
        trc._SESSION_CACHE.clear()

    def test_a_throttled_account_still_counts_as_working(self, monkeypatch):
        """The cap is PER USER, so a 429 proves TestRail authenticated the
        account before throttling it.  Rejecting it dropped healthy accounts
        whenever the startup window happened to be closed — halving the pool
        exactly when it was needed most."""
        from src import testrail_client as trc

        class _Resp:
            status_code, ok, text = 429, False, (
                '{"error":"API Rate Limit Exceeded - 5 per minute maximum allowed."}')

        monkeypatch.setattr(trc.TestRailClient, "_make_session",
                            lambda self, c: SimpleNamespace(
                                get=lambda *a, **k: _Resp()))
        assert trc._account_works(
            trc.TestRailCredentials("https://x", "u@x", "k")) is True

    def test_a_rejected_credential_is_still_dropped(self, monkeypatch):
        """The tolerance above must not swallow a genuinely bad secret."""
        from src import testrail_client as trc

        class _Resp:
            status_code, ok, text = 401, False, "Authentication failed"

        monkeypatch.setattr(trc.TestRailClient, "_make_session",
                            lambda self, c: SimpleNamespace(
                                get=lambda *a, **k: _Resp()))
        assert trc._account_works(
            trc.TestRailCredentials("https://x", "u@x", "k")) is False

    def test_the_pacer_follows_the_WORKING_count(self, monkeypatch):
        """Pacing for four while three answer would push each over its own cap."""
        from src import testrail_client as trc
        trc._SESSION_CACHE.clear()
        creds = [trc.TestRailCredentials("https://x", f"u{i}@x", "k")
                 for i in range(4)]
        monkeypatch.setattr(trc, "_credential_sets", lambda: creds)
        monkeypatch.setattr(trc, "_account_works", lambda c: c.user != "u2@x")
        monkeypatch.setattr(trc.TestRailClient, "_make_session",
                            lambda self, c: object())
        trc._get_client()
        assert (60 / trc._PACE_INTERVAL) / trc.n_workers() <= trc._effective_limit()
        trc._SESSION_CACHE.clear()
