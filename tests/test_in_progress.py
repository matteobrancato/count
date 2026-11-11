"""Where a row goes when its status says "In progress", and when a TestIM field
may overrule the generic one.

Matteo, 2026-11-02, answering the QA's question about TPS and IPXL:
- "In progress" is not Backlog: it is shown beside it, in the Outstanding stack;
- a filled TestIM device field decides its device's row over the generic field,
  even over an N/A written by hand — but not on a case labelled `playwright`,
  where the generic field decides again;
- generic "To be updated" with TestIM "In progress" is, for now, In progress.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src import automation_save
from src.ui import backlog_tab as bl

GEN = "status_Automation Status"
TD = "status_Automation Status Testim Desktop"
TM = "status_Automation Status Testim Mobile View"
RULES = [r for r in bl.ALL_RULES if r.bu == "Drogas" and r.scope == "website"]
BOTH = ["big_regr_desktop", "big_regr_mobile"]


def _rows(*, generic=None, td=None, tm=None, labels=BOTH):
    raw = pd.DataFrame([{
        "case_id": 1, "suite_id": 16093, "labels": list(labels), "type_label": "Functional",
        "multi_countries": ["LV"], "priority_label": "High",
        GEN: generic, TD: td, TM: tm}])
    exp = bl._expand_baseline(raw, RULES)
    return dict(zip(exp["device"], exp["_cat_base"]))


# ── In progress is a category of its own ─────────────────────────────────────
class TestInProgress:
    @pytest.mark.parametrize("text", ["In progress", "In Progress", " in progress "])
    def test_the_generic_field_puts_every_row_in_progress(self, text):
        assert _rows(generic=text) == {"Desktop": "in_progress", "Mobile": "in_progress"}

    def test_a_device_field_decides_its_own_device(self):
        assert _rows(generic="Ready to be automated", td="In progress") == {
            "Desktop": "in_progress", "Mobile": "backlog"}
        assert _rows(generic="Ready to be automated", tm="In progress") == {
            "Desktop": "backlog", "Mobile": "in_progress"}

    def test_it_is_not_backlog_in_the_case_wide_pass_either(self):
        """Backlog = not automated and not started; a script in progress is out."""
        assert "backlog" not in _rows(generic="In progress", td="In progress").values()

    def test_in_progress_on_one_device_does_not_leak_to_the_other(self):
        """Like an N/A written for one device: a decision about that device."""
        assert _rows(generic=None, td="In progress") == {
            "Desktop": "in_progress", "Mobile": "backlog"}

    def test_generic_to_be_updated_with_testim_in_progress_is_in_progress(self):
        """IPXL: 9 cases, 17 rows.  Matteo: "per ora mettiamolo come in progress"."""
        assert _rows(generic="To be updated", td="In progress") == {
            "Desktop": "in_progress", "Mobile": "to_be_updated"}

    def test_testim_to_be_updated_still_wins_for_its_device(self):
        assert _rows(generic="In progress", td="To be updated")["Desktop"] == "to_be_updated"

    def test_n_a_still_beats_in_progress_in_the_case_wide_pass(self):
        assert _rows(generic="Automation not applicable")["Desktop"] == "not_applicable"

    def test_an_automated_row_stays_automated(self):
        """A script that exists for this row wins, as it does over Backlog."""
        raw = pd.DataFrame([{
            "case_id": 1, "suite_id": 16093, "labels": ["big_regr_desktop"], "type_label": "Functional",
            "multi_countries": ["LV"], "priority_label": "High", GEN: "Automated", TD: "In progress", TM: None}])
        exp = bl._expand_baseline(raw, RULES)
        auto = pd.DataFrame([{"case_id": 1, "country_label": "LV", "device": "Desktop",
                              "framework": "java"}])
        assert list(bl._classify_expanded(exp, auto)["category"]) == ["automated"]

    def test_the_mobile_app_reads_it_the_same_way(self):
        rules = [r for r in bl.ALL_RULES if r.bu == "Drogas" and r.scope == "mobile_app"]
        raw = pd.DataFrame([{"case_id": 1, "suite_id": rules[0].suite_id, "labels": [],
                             "priority_label": "High", GEN: "In progress"}])
        exp = bl._expand_mapp_baseline(raw, rules)
        assert set(exp["_cat_base"]) == {"in_progress"} and len(exp) == 4


# ── TestIM over the generic field, except on a Playwright case ───────────────
class TestTestimOverGeneric:
    def test_testim_not_automated_beats_a_generic_n_a(self):
        """TPS: 9 cases.  "Evidentemente non era automatable prima ma forse con
        Testim si".  Until 2026-11-04 only on its own device — Desktop Backlog,
        Mobile N/A, one case in two categories.  Matteo: "desktop e mobile vale come
        backlog ugualmente se non specificato ulteriormente in testim, perche' vuol
        dire che non e' stato ancora preso in carico"."""
        assert _rows(generic="Automation not applicable", td="Not automated") == {
            "Desktop": "backlog", "Mobile": "backlog"}
        assert _rows(generic="Automation not applicable", td="Not automated",
                     tm="Automation not applicable") == {
            "Desktop": "backlog", "Mobile": "not_applicable"}

    def test_a_playwright_case_goes_back_to_the_generic_field(self):
        assert _rows(generic="Automation not applicable", td="Not automated",
                     labels=BOTH + ["playwright"]) == {
            "Desktop": "not_applicable", "Mobile": "not_applicable"}

    def test_the_label_is_matched_like_the_engine_matches_it(self):
        assert _rows(generic="Automation not applicable", td="Not automated",
                     labels=BOTH + ["Playwright"])["Desktop"] == "not_applicable"

    def test_a_playwright_case_ignores_testim_in_progress_too(self):
        assert _rows(generic="Ready to be automated", td="In progress",
                     labels=BOTH + ["playwright"]) == {"Desktop": "backlog", "Mobile": "backlog"}

    def test_a_playwright_case_with_a_generic_backlog_status_is_backlog(self):
        """Matteo: "se c'e' la label playwright e il campo automation status generico
        e' uno stato di backlog, vale come backlog, si"."""
        assert _rows(generic="Not automated", td="To be updated",
                     labels=BOTH + ["playwright"]) == {"Desktop": "backlog", "Mobile": "backlog"}

    def test_a_playwright_case_in_progress_on_the_generic_field_is_in_progress(self):
        assert _rows(generic="In progress", labels=BOTH + ["playwright"])["Desktop"] == "in_progress"

    def test_the_case_without_the_label_is_untouched_by_all_this(self):
        assert _rows(generic="Ready to be automated", td="Not automated") == {
            "Desktop": "backlog", "Mobile": "backlog"}
        assert _rows(generic="Automation not applicable", td="Automation not applicable") == {
            "Desktop": "not_applicable", "Mobile": "not_applicable"}

    def test_whether_a_script_exists_still_reads_the_raw_testim_field(self):
        """The label changes how a row is CLASSIFIED, not whether the case has a
        script: a Playwright case whose old TestIM field says Automated UAT is
        still a case with a script (Partially Automated, not Backlog, where its
        automated rows do not cover this one)."""
        raw = pd.DataFrame([{
            "case_id": 1, "suite_id": 16093, "labels": BOTH + ["playwright"], "type_label": "Functional",
            "multi_countries": ["LV"], "priority_label": "High",
            GEN: "Ready to be automated", TD: "Automated UAT", TM: None}])
        exp = bl._expand_baseline(raw, RULES)
        assert exp["_tool_automated"].all()


class TestManyCasesManyCountries:
    """The expansion repeats the index (one row per country, then per device); a
    mask built before it no longer lines up.  Live, 2026-11-02, this was an
    IndexError on the real data and passed every one-case test."""

    def test_each_case_keeps_its_own_verdict(self):
        raw = pd.DataFrame([
            {"case_id": 1, "suite_id": 16093, "labels": BOTH, "type_label": "Functional",
             "multi_countries": ["LV", "LT"], "priority_label": "High",
             GEN: "Automation not applicable", TD: "Not automated", TM: None},
            {"case_id": 2, "suite_id": 16093, "labels": BOTH + ["playwright"], "type_label": "Functional",
             "multi_countries": ["LV", "LT"], "priority_label": "High",
             GEN: "Automation not applicable", TD: "Not automated", TM: None},
            {"case_id": 3, "suite_id": 16093, "labels": BOTH, "type_label": "Functional",
             "multi_countries": ["LV"], "priority_label": "High",
             GEN: "To be updated", TD: "In progress", TM: "In progress"},
            {"case_id": 4, "suite_id": 16093, "labels": ["big_regr_mobile"], "type_label": "Functional",
             "multi_countries": ["LT", "LV"], "priority_label": "High",
             GEN: "Ready to be automated", TD: None, TM: None},
        ])
        exp = bl._expand_baseline(raw, RULES)
        got = {(int(r["case_id"]), r["device"]): r["_cat_base"] for r in exp.to_dict("records")}
        assert got == {
            (1, "Desktop"): "backlog", (1, "Mobile"): "backlog",
            (2, "Desktop"): "not_applicable", (2, "Mobile"): "not_applicable",
            (3, "Desktop"): "in_progress", (3, "Mobile"): "in_progress",
            (4, "Mobile"): "backlog"}
        assert len(exp) == 2 * 2 + 2 * 2 + 1 * 2 + 2 * 1     # countries x devices per case

    def test_the_helper_columns_do_not_leak_into_the_result(self):
        raw = pd.DataFrame([{"case_id": 1, "suite_id": 16093, "labels": BOTH, "type_label": "Functional",
                             "multi_countries": ["LV"], "priority_label": "High", GEN: "In progress",
                             TD: None, TM: None}])
        assert "_is_pw" not in bl._expand_baseline(raw, RULES).columns


class TestAgainstAPlainRestatement:
    """The same rules written out case by case, against a few hundred random cases
    with every kind of gap in them — the shape of real TestRail data, which the
    hand-made cases above are not."""

    VALUES = [None, "", "Automated", "Automated UAT", "Not automated", "Ready to be automated",
              "In progress", "To be updated", "Automation not applicable", "Blocked"]
    AUTO = {"Automated", "Automated DEV", "Automated UAT", "Automated Prod"}

    @classmethod
    def _expected(cls, c, device):
        blank = lambda v: v in (None, "")                                    # noqa: E731
        pw = any(str(x).lower() == "playwright" for x in c["labels"])
        gen, td, tm = c[GEN], (None if pw else c[TD]), (None if pw else c[TM])
        tbu = lambda v: str(v).strip().lower() == "to be updated"            # noqa: E731
        ip = lambda v: str(v).strip().lower() == "in progress"               # noqa: E731
        cat = "unknown"
        if any(not blank(v) and v not in cls.AUTO | {"Automation not applicable"}
               and not tbu(v) and not (ip(v) and v is gen) for v in (gen, td, tm)):
            cat = "backlog"
        if any(tbu(v) for v in (gen, td, tm)):
            cat = "to_be_updated"
        if ip(gen):
            cat = "in_progress"
        if gen == "Automation not applicable":
            cat = "not_applicable"
        own = td if device == "Desktop" else tm
        other = tm if device == "Desktop" else td
        def not_taken(v):
            return (not blank(v) and v not in cls.AUTO | {"Automation not applicable"}
                    and not tbu(v) and not ip(v))
        if not blank(own):
            if own == "Automation not applicable":
                cat = "not_applicable"
            elif ip(own):
                cat = "in_progress"
            elif tbu(own):
                cat = "to_be_updated"
            elif own not in cls.AUTO and not tbu(gen):      # a generic "To be updated" holds
                cat = "backlog"
        elif cat == "not_applicable" and not_taken(other):
            cat = "backlog"                                  # "not taken in charge" speaks for the case
        return cat

    def test_three_hundred_random_cases(self):
        import random
        rng = random.Random(20261102)
        cases = []
        for i in range(300):
            labels = rng.choice([BOTH, ["big_regr_desktop"], ["big_regr_mobile"]])
            if rng.random() < 0.3:
                labels = labels + [rng.choice(["playwright", "Playwright"])]
            cases.append({
                "case_id": i + 1, "suite_id": 16093, "labels": labels, "type_label": "Functional",
                "multi_countries": rng.choice([["LV"], ["LT"], ["LV", "LT"], ["LV", "LT", "RU"]]),
                "priority_label": "High", GEN: rng.choice(self.VALUES),
                TD: rng.choice(self.VALUES), TM: rng.choice(self.VALUES)})
        exp = bl._expand_baseline(pd.DataFrame(cases), RULES)
        by_id = {c["case_id"]: c for c in cases}
        assert len(exp) > 300
        wrong = [(r["case_id"], r["device"], r["_cat_base"], self._expected(by_id[r["case_id"]], r["device"]))
                 for r in exp.to_dict("records")
                 if r["_cat_base"] != self._expected(by_id[r["case_id"]], r["device"])]
        assert wrong == []


# ── the numbers around it ────────────────────────────────────────────────────
def _classified(categories):
    exp = pd.DataFrame({
        "case_id": range(len(categories)), "country_label": "LV", "device": "Desktop",
        "category": categories})
    return exp, pd.DataFrame(columns=bl._AUTO_SLIM_COLS)


class TestStats:
    CATS = ["automated"] * 6 + ["backlog"] * 2 + ["in_progress"] * 3 + ["to_be_updated"] + \
           ["partially_automated"] + ["not_applicable"] * 2

    def test_the_total_is_still_the_sum_of_the_categories(self):
        s = bl._stats(*_classified(self.CATS))
        assert s["total"] == (s["automated"] + s["backlog"] + s["in_progress"]
                              + s["partially_automated"] + s["to_be_updated"]
                              + s["not_applicable"] + s["unknown"])
        assert (s["backlog"], s["in_progress"]) == (2, 3)

    def test_coverage_does_not_move_when_a_row_leaves_the_backlog(self):
        """Taking "In progress" out of Backlog changes no ratio: it is automatable
        work like the rest."""
        a = bl._stats(*_classified(["automated"] * 6 + ["backlog"] * 5))
        b = bl._stats(*_classified(["automated"] * 6 + ["backlog"] * 2 + ["in_progress"] * 3))
        for key in ("cov_total", "cov_automatable", "cov_ex_partial", "na_pct"):
            assert a[key] == b[key], key

    def test_the_unique_cases_are_counted(self):
        assert bl._stats(*_classified(self.CATS))["u_ip"] == 3


class TestTable:
    def _df(self, ip):
        return pd.DataFrame([{"BU": "Drogas", "Scope": "Website", "Total": 10, "Automated": 5,
                              "Backlog": 2, "In Progress": ip, "Partially Automated": 1,
                              "To be Updated": 1, "Not Applicable": 1, "Coverage %": 50.0}])

    COLS = ["Total", "Automated", "Backlog", "In Progress", "Partially Automated", "To be Updated"]

    def test_the_stack_shows_it_between_backlog_and_to_be_updated(self):
        out = bl._summary_table_html(self._df(3), self.COLS)
        lines = [out.index(f"<i>{x}</i>") for x in ("BACKLOG", "In progress", "To be Updated", "Partially")
                 if f"<i>{x}</i>" in out]
        assert "<i>In progress</i>" in out and lines == sorted(lines)
        assert out.index("<i>Backlog</i>") < out.index("<i>In progress</i>") < out.index("<i>To be Updated</i>")

    def test_a_table_without_any_has_no_such_line(self):
        cols = [c for c in self.COLS if c != "In Progress"]
        assert "In progress" not in bl._summary_table_html(self._df(0), cols)

    def test_the_stack_order_is_the_workflow(self):
        assert bl._STACK_COLS == ["Backlog", "In Progress", "To be Updated", "Partially Automated"]

    def test_the_summary_always_carries_the_column(self):
        assert "In Progress" in bl._SUMMARY_COLUMNS


class TestExportsAndRecipe:
    def test_the_tile_is_an_export_category_the_classifier_can_produce(self):
        assert ("in_progress", "In Progress") in bl._EXPORT_CATEGORIES
        assert bl._CATEGORY_LABELS["in_progress"] == "In Progress"

    def test_its_rows_come_out_of_the_evidence_frame(self):
        ev = pd.DataFrame({"Case ID": [1, 2, 3], "_cat": ["in_progress", "backlog", "in_progress"]})
        assert list(bl._rows_for_category(ev, "in_progress")["Case ID"]) == [1, 3]

    def test_the_deciding_field_names_the_status_that_said_in_progress(self):
        case = {GEN: "Ready to be automated", TD: "In progress"}
        assert bl._deciding_field(case, "Desktop", "in_progress") == (
            "Automation Status Testim Desktop", "In progress")

    def test_backlog_is_never_decided_by_an_in_progress_value(self):
        case = {GEN: "In progress", TD: "Not automated"}
        assert bl._deciding_field(case, "Desktop", "backlog") == (
            "Automation Status Testim Desktop", "Not automated")

    def test_the_recipe_says_where_in_progress_goes(self):
        text = " ".join(bl._filter_recipe("Drogas", "website", "backlog", 1, 1)["Filter"])
        assert "In progress" in text
        text = " ".join(bl._filter_recipe("Drogas", "website", "in_progress", 1, 1)["Filter"])
        assert "In progress" in text


class TestAutomationSaveKeepsItsFigure:
    def test_the_table_hands_the_save_the_whole_unfinished_work(self, monkeypatch):
        """In progress is still to be automated: moving rows out of Backlog must not
        shrink what automating the rest would save."""
        seen = []
        monkeypatch.setattr(bl.automation_save, "config_for",
                            lambda bu, scope: automation_save.Config("MD", "", 1.0))
        monkeypatch.setattr(bl.automation_save, "compute",
                            lambda coeff, auto, backlog, total, by: seen.append((auto, backlog, total)))
        df = pd.DataFrame([{"BU": "Drogas", "Total": 100, "Automated": 70, "Backlog": 10,
                            "In Progress": 5}])
        bl._summary_saves(df, "website")
        assert seen == [(70, 15, 100)]
