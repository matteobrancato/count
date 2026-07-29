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
        _nd, _ab, _ids, expanded_cov = cov._regression_baseline_like_backlog(
            raw, auto, rules)

        assert len(expanded_cov) == s["total"] == 5          # 2+2+1 rows
        assert int((expanded_cov["category"] == "automated").sum()) == s["automated"] == 2
        cov_pct = (expanded_cov["category"] == "automated").sum() / len(expanded_cov) * 100
        assert round(cov_pct, 1) == round(s["cov_total"], 1) == 40.0

    def test_per_area_denominator_is_rows_not_cases(self, website_rule):
        raw, auto, rules = self._fixture(website_rule)
        _nd, ab, ids, expanded_cov = cov._regression_baseline_like_backlog(
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
        _nd, ab, ids, _exp = cov._regression_baseline_like_backlog(raw, auto, rules)
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
                    "Country Coverage", "Countries (multi_countries)",
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
