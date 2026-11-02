"""Martech: the backlog of the Testim automation plan, read from a second
TestRail instance.  The numbers come from one plan and no label, so what is
tested here is that none of them can be lost, double-counted or invented on the
way from TestRail's counts to the table: every test lands in exactly one BU row,
every row adds up, and a failure to read is an error, never a zero."""
from __future__ import annotations

import types

import pytest
import streamlit as st

from src import martech as mt

STATUSES = {1: "Passed", 2: "Blocked", 3: "To be tested", 4: "Retest", 5: "Failed",
            6: "On hold", 7: "N/A", 8: "Accepted fail", 9: "In Progress", 10: "Reviewed"}


def _run(run_id, name, **counts):
    """A run object as get_plan returns it: TestRail's own count keys."""
    base = {"id": run_id, "name": name}
    base.update({k: 0 for k in mt._COUNT_KEYS})
    base.update(counts)
    return base


def _plan(*runs):
    return {"name": "Testim Automation BACKLOG",
            "entries": [{"name": r["name"], "runs": [r]} for r in runs]}


@pytest.fixture
def plan():
    return _plan(
        _run(1, "KV NL", passed_count=2, blocked_count=5, untested_count=3, custom_status2_count=1),
        _run(2, "KV BE", blocked_count=4, untested_count=1),
        _run(3, "WTR", passed_count=1, custom_status4_count=2, custom_status1_count=6),
        _run(4, "MFR", custom_status1_count=7),
        _run(5, "MCH", custom_status1_count=3, passed_count=1),
        _run(6, "Brand New 1", untested_count=4),
    )


# ── the plan's shape ─────────────────────────────────────────────────────────
class TestRunsInPlan:
    def test_counts_are_read_by_status_id(self):
        run = mt.runs_in_plan(_plan(_run(9, "SD", passed_count=1, blocked_count=2,
                                         untested_count=3, retest_count=4, failed_count=5,
                                         custom_status1_count=6, custom_status7_count=7)))[0]
        assert run.counts == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 12: 7}

    def test_zero_counts_are_left_out(self):
        assert mt.runs_in_plan(_plan(_run(9, "SD", blocked_count=2)))[0].counts == {2: 2}

    def test_every_run_of_every_entry_is_read(self):
        plan = {"entries": [{"name": "KV", "runs": [_run(1, "KV NL", blocked_count=1),
                                                    _run(2, "KV BE", blocked_count=1)]},
                            {"name": "SD", "runs": [_run(3, "SD", blocked_count=1)]}]}
        assert [r.run_id for r in mt.runs_in_plan(plan)] == [1, 2, 3]

    def test_a_run_without_a_name_takes_its_entry_s(self):
        run = _run(1, "", blocked_count=1)
        run["name"] = None
        assert mt.runs_in_plan({"entries": [{"name": "WTR", "runs": [run]}]})[0].name == "WTR"

    def test_an_empty_plan_has_no_run(self):
        assert mt.runs_in_plan({}) == []


class TestBuOfRun:
    @pytest.mark.parametrize("run,bu", [
        ("IPXL NL", "ICI Paris XL"), ("IPXL BE", "ICI Paris XL"), ("KV NL", "Kruidvat"),
        ("WTR", "Watsons Turkey"), ("Trekpleister", "Trekpleister"), ("DRG LV", "Drogas"),
        ("SD", "Superdrug"), ("SV", "Savers"), ("TPS UK", "The Perfume Shop"),
        ("MFR", "Marionnaud"), ("MHU", "Marionnaud"), ("mch", "Marionnaud"),
        ("tps ie", "The Perfume Shop"),
    ])
    def test_known_runs(self, run, bu):
        assert mt.bu_of_run(run) == bu

    def test_an_unknown_run_is_its_own_row(self):
        """A test is never dropped for want of a mapping."""
        assert mt.bu_of_run("Brand New 1") == "Brand New 1"
        assert mt.bu_of_run("") == ""

    def test_every_known_bu_is_listed(self):
        assert set(mt.KNOWN_BUS) == {"ICI Paris XL", "Kruidvat", "Trekpleister",
                                     "Watsons Turkey", "Drogas", "Superdrug", "Savers",
                                     "The Perfume Shop", "Marionnaud"}


# ── the table's numbers ──────────────────────────────────────────────────────
class TestSummary:
    def test_no_test_is_lost_or_counted_twice(self, plan):
        runs = mt.runs_in_plan(plan)
        table = mt.summary(runs, STATUSES)
        assert table["Total"].sum() == sum(r.total for r in runs) == 40

    def test_every_row_adds_up(self, plan):
        table = mt.summary(mt.runs_in_plan(plan), STATUSES)
        for _, r in table.iterrows():
            assert r["Merged"] + r["Backlog"] + r["Not Applicable"] == r["Total"], r["BU"]

    def test_backlog_is_every_status_but_passed_and_na(self, plan):
        table = mt.summary(mt.runs_in_plan(plan), STATUSES).set_index("BU")
        kv = table.loc["Kruidvat"]
        assert (kv["Total"], kv["Merged"], kv["Not Applicable"], kv["Backlog"]) == (16, 2, 1, 13)

    def test_a_status_column_is_the_sum_of_that_status(self, plan):
        table = mt.summary(mt.runs_in_plan(plan), STATUSES).set_index("BU")
        assert table.loc["Kruidvat", "Blocked"] == 9
        assert table.loc["Marionnaud", "On hold"] == 10
        # and the columns of the open statuses add up to the backlog
        status_cols = [c for c in table.columns if c in STATUSES.values()
                       and c not in ("Passed", "N/A")]
        assert (table[status_cols].sum(axis=1) == table["Backlog"]).all()

    def test_only_statuses_that_occur_are_columns(self, plan):
        cols = mt.summary(mt.runs_in_plan(plan), STATUSES).columns
        assert "Retest" not in cols and "Reviewed" not in cols and "Failed" not in cols
        assert "In Progress" in cols and "On hold" in cols

    def test_columns_follow_the_flow_of_the_work(self, plan):
        cols = list(mt.summary(mt.runs_in_plan(plan), STATUSES).columns)
        flow = [c for c in cols if c in ("To be tested", "In Progress", "Blocked", "On hold")]
        assert flow == ["To be tested", "In Progress", "Blocked", "On hold"]

    def test_a_status_nobody_planned_for_still_gets_a_column(self):
        runs = mt.runs_in_plan(_plan(_run(1, "SD", custom_status7_count=3)))
        table = mt.summary(runs, {**STATUSES, 12: "Waiting for API"})
        assert table.loc[0, "Waiting for API"] == 3 and table.loc[0, "Backlog"] == 3

    def test_an_unnamed_status_is_shown_by_its_id_not_dropped(self):
        runs = mt.runs_in_plan(_plan(_run(1, "SD", custom_status7_count=3)))
        assert mt.summary(runs, STATUSES).loc[0, "Status 12"] == 3

    def test_na_is_found_by_its_label(self):
        """Ids belong to one instance; a renamed status follows its name."""
        runs = mt.runs_in_plan(_plan(_run(1, "SD", blocked_count=2, custom_status2_count=1)))
        renamed = {**STATUSES, 7: "Not applicable"}
        assert mt.summary(runs, renamed).loc[0, "Not Applicable"] == 1
        gone = {k: v for k, v in STATUSES.items() if k != 7}
        row = mt.summary(runs, gone).loc[0]
        assert row["Not Applicable"] == 0 and row["Backlog"] == 3    # kept in the backlog

    def test_unknown_runs_get_a_row_of_their_own(self, plan):
        bus = list(mt.summary(mt.runs_in_plan(plan), STATUSES)["BU"])
        assert "Brand New 1" in bus

    def test_rows_follow_the_order_given_then_alphabetically(self, plan):
        order = ["Watsons Turkey", "Kruidvat"]
        bus = list(mt.summary(mt.runs_in_plan(plan), STATUSES, order=order)["BU"])
        assert bus[:2] == ["Watsons Turkey", "Kruidvat"]
        assert bus[2:] == sorted(bus[2:])

    def test_an_empty_plan_is_an_empty_table(self):
        assert mt.summary([], STATUSES).empty

    def test_the_runs_of_a_bu_are_counted(self, plan):
        table = mt.summary(mt.runs_in_plan(plan), STATUSES).set_index("BU")
        assert table.loc["Kruidvat", "Runs"] == 2 and table.loc["Watsons Turkey", "Runs"] == 1


class TestProgress:
    def test_it_is_merged_over_what_is_left_after_na(self):
        assert mt.progress({"Total": 12, "Merged": 2, "Backlog": 9, "Not Applicable": 1}) == 18.2

    def test_a_bu_with_only_na_has_no_progress_not_a_crash(self):
        assert mt.progress({"Total": 3, "Merged": 0, "Backlog": 0, "Not Applicable": 3}) == 0.0

    def test_it_reaches_a_hundred(self):
        assert mt.progress({"Total": 4, "Merged": 4, "Backlog": 0, "Not Applicable": 0}) == 100.0


class TestOneBu:
    def test_runs_summary_has_one_row_per_run(self, plan):
        frame = mt.runs_summary(mt.runs_in_plan(plan), STATUSES, "Kruidvat")
        assert list(frame["Run"]) == ["KV NL", "KV BE"]
        assert "Runs" not in frame.columns
        assert frame["Total"].sum() == 16

    def test_bu_figures_match_the_table_row(self, plan):
        runs = mt.runs_in_plan(plan)
        row = mt.summary(runs, STATUSES).set_index("BU").loc["Marionnaud"]
        figures = mt.bu_figures(runs, STATUSES, "Marionnaud")
        assert figures == {k: int(row[k]) for k in ("Total", "Merged", "Backlog", "Not Applicable")}

    def test_open_statuses_of_a_bu_are_only_its_own(self, plan):
        runs = [r for r in mt.runs_in_plan(plan) if r.bu == "Marionnaud"]
        assert mt.open_statuses(runs, STATUSES) == [6]


class TestTestsFrame:
    def test_every_test_is_a_row_with_a_case_link(self, plan):
        runs = mt.runs_in_plan(plan)[:2]
        tests = {1: [{"test_id": 10, "case_id": 777, "title": "Login", "status_id": 1}],
                 2: [{"test_id": 11, "case_id": 778, "title": "Pay", "status_id": 2}]}
        frame = mt.tests_frame(runs, tests, STATUSES, "https://x.example")
        assert list(frame.columns) == ["Case", "Title", "Run", "Status"]
        assert frame.loc[0, "Case"] == "https://x.example/index.php?/cases/view/777"
        assert list(frame["Run"]) == ["KV NL", "KV BE"]
        assert list(frame["Status"]) == ["Passed", "Blocked"]

    def test_no_tests_is_an_empty_frame_with_its_columns(self):
        frame = mt.tests_frame([], {}, STATUSES, "https://x.example")
        assert frame.empty and list(frame.columns) == ["Case", "Title", "Run", "Status"]


# ── configuration ────────────────────────────────────────────────────────────
class TestConfiguration:
    def _secrets(self, monkeypatch, **values):
        monkeypatch.setattr(st, "secrets", values, raising=False)

    def test_not_configured_without_the_key(self, monkeypatch):
        self._secrets(monkeypatch, TESTRAIL_USER="someone@example.com")
        assert mt.configured() is False

    def test_configured_with_the_key(self, monkeypatch):
        self._secrets(monkeypatch, MARTECH_TESTRAIL_API_KEY="k")
        assert mt.configured() is True

    def test_a_blank_key_is_not_a_key(self, monkeypatch):
        self._secrets(monkeypatch, MARTECH_TESTRAIL_API_KEY="   ")
        assert mt.configured() is False

    def test_no_secrets_at_all_is_not_configured(self, monkeypatch):
        class Missing:
            def get(self, *_):
                raise FileNotFoundError("no secrets.toml")
        monkeypatch.setattr(st, "secrets", Missing(), raising=False)
        assert mt.configured() is False

    def test_the_user_falls_back_to_the_main_account(self, monkeypatch):
        self._secrets(monkeypatch, MARTECH_TESTRAIL_API_KEY="k", TESTRAIL_USER="me@example.com")
        creds = mt.credentials()
        assert (creds.user, creds.base_url) == ("me@example.com", "https://aswatson.testrail.io")

    def test_its_own_user_and_url_win(self, monkeypatch):
        self._secrets(monkeypatch, MARTECH_TESTRAIL_API_KEY="k", MARTECH_TESTRAIL_USER="mt@example.com",
                      TESTRAIL_USER="me@example.com", MARTECH_TESTRAIL_URL="https://other.example/")
        creds = mt.credentials()
        assert (creds.user, creds.base_url) == ("mt@example.com", "https://other.example")

    def test_the_main_accounts_naming_works_too(self, monkeypatch):
        """TESTRAIL_API_KEY_MARTECH is what follows from TESTRAIL_API_KEY_1."""
        self._secrets(monkeypatch, TESTRAIL_API_KEY_MARTECH="k", TESTRAIL_USER_MARTECH="mt@example.com",
                      TESTRAIL_URL_MARTECH="https://other.example")
        assert mt.configured() is True
        creds = mt.credentials()
        assert (creds.user, creds.base_url, creds.api_key) == ("mt@example.com",
                                                               "https://other.example", "k")

    def test_the_documented_name_wins_when_both_are_set(self, monkeypatch):
        self._secrets(monkeypatch, MARTECH_TESTRAIL_API_KEY="first", TESTRAIL_API_KEY_MARTECH="second",
                      TESTRAIL_USER="me@example.com")
        assert mt.credentials().api_key == "first"

    def test_no_key_is_an_error_that_names_it(self, monkeypatch):
        self._secrets(monkeypatch, TESTRAIL_USER="me@example.com")
        with pytest.raises(mt.MartechError, match="MARTECH_TESTRAIL_API_KEY"):
            mt.credentials()

    def test_no_user_anywhere_is_an_error(self, monkeypatch):
        self._secrets(monkeypatch, MARTECH_TESTRAIL_API_KEY="k")
        with pytest.raises(mt.MartechError, match="TESTRAIL_USER"):
            mt.credentials()

    def test_the_key_is_never_printed(self, monkeypatch):
        self._secrets(monkeypatch, MARTECH_TESTRAIL_API_KEY="s3cr3t-key", TESTRAIL_USER="me@example.com")
        assert "s3cr3t-key" not in repr(mt.credentials())

    def test_the_plan_defaults_and_can_be_overridden(self, monkeypatch):
        self._secrets(monkeypatch)
        assert mt.plan_id() == 3721
        self._secrets(monkeypatch, MARTECH_PLAN_ID="4000")
        assert mt.plan_id() == 4000

    def test_a_plan_id_that_is_not_a_number_says_so(self, monkeypatch):
        self._secrets(monkeypatch, MARTECH_PLAN_ID="three")
        with pytest.raises(mt.MartechError, match="MARTECH_PLAN_ID"):
            mt.plan_id()


# ── the client ───────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code, self._body, self.text = status, body, text
        self.headers = headers or {}
        self.ok = status < 400

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class TestClient:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setattr(st, "secrets", {"MARTECH_TESTRAIL_API_KEY": "s3cr3t-key",
                                            "TESTRAIL_USER": "me@example.com"}, raising=False)
        monkeypatch.setattr(mt.time, "sleep", lambda _s: None)
        monkeypatch.setattr(mt, "_wait_turn", lambda: None)
        monkeypatch.setattr(mt, "_interval", 0.35)
        self.calls = []

    def _script(self, monkeypatch, *responses):
        queue = list(responses)

        def get(url, timeout):
            self.calls.append(url)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        monkeypatch.setattr(mt, "_session", lambda user, key: types.SimpleNamespace(get=get))

    def test_it_returns_the_json(self, monkeypatch):
        self._script(monkeypatch, _Resp(body={"ok": 1}))
        assert mt._get("get_plan/1") == {"ok": 1}
        assert self.calls == ["https://aswatson.testrail.io/index.php?/api/v2/get_plan/1"]

    def test_a_refused_key_says_so_without_printing_it(self, monkeypatch):
        self._script(monkeypatch, _Resp(status=401, text="denied"))
        with pytest.raises(mt.MartechError) as err:
            mt._get("get_plan/1")
        msg = str(err.value)
        assert "401" in msg and "MARTECH_TESTRAIL_API_KEY" in msg and "aswatson.testrail.io" in msg
        assert "s3cr3t-key" not in msg

    def test_a_429_is_waited_out_and_retried(self, monkeypatch):
        self._script(monkeypatch, _Resp(status=429, text="5 per minute maximum allowed"),
                     _Resp(body=[1]))
        assert mt._get("get_statuses") == [1]
        assert len(self.calls) == 2

    def test_the_pace_follows_the_cap_a_429_states(self, monkeypatch):
        self._script(monkeypatch, _Resp(status=429, text="5 per minute maximum allowed"),
                     _Resp(body=[1]))
        mt._get("get_statuses")
        assert mt._interval == pytest.approx(15.0)       # 60 / (5 * 0.8)

    def test_a_429_that_names_no_cap_slows_down_anyway(self, monkeypatch):
        self._script(monkeypatch, _Resp(status=429, text="slow down"), _Resp(body=[1]))
        mt._get("get_statuses")
        assert mt._interval == pytest.approx(0.7)

    def test_a_limit_that_never_lifts_is_an_error_not_a_zero(self, monkeypatch):
        self._script(monkeypatch, *[_Resp(status=429, text="x")] * mt._ATTEMPTS)
        with pytest.raises(mt.MartechError, match="rate limit"):
            mt._get("get_plan/1")

    def test_other_http_errors_carry_the_status(self, monkeypatch):
        self._script(monkeypatch, _Resp(status=500, text="boom"))
        with pytest.raises(mt.MartechError, match="500"):
            mt._get("get_plan/1&limit=250")

    def test_a_network_failure_is_retried_then_reported(self, monkeypatch):
        import requests
        self._script(monkeypatch, *[requests.ConnectionError("x")] * mt._ATTEMPTS)
        with pytest.raises(mt.MartechError, match="not reachable"):
            mt._get("get_plan/1")
        assert len(self.calls) == mt._ATTEMPTS

    def test_an_answer_that_is_not_json_is_an_error(self, monkeypatch):
        self._script(monkeypatch, _Resp(status=200, body=None))
        with pytest.raises(mt.MartechError, match="not JSON"):
            mt._get("get_plan/1")


class TestFetches:
    def test_the_plan_comes_with_when_it_was_read(self, monkeypatch):
        monkeypatch.setattr(st, "secrets", {"MARTECH_TESTRAIL_API_KEY": "k",
                                            "TESTRAIL_USER": "me@example.com"}, raising=False)
        monkeypatch.setattr(mt, "_get", lambda endpoint: {"asked": endpoint})
        out = mt.fetch_plan.__wrapped__()
        assert out["plan"] == {"asked": "get_plan/3721"}
        assert out["base_url"] == "https://aswatson.testrail.io" and out["read_at"] > 0

    def test_statuses_are_keyed_by_id_and_named_by_label(self, monkeypatch):
        monkeypatch.setattr(mt, "_get", lambda endpoint: [
            {"id": 1, "name": "passed", "label": "Passed"}, {"id": 7, "name": "status_7", "label": "N/A"},
            {"id": 11, "name": "status_11"}])
        assert mt.fetch_statuses.__wrapped__() == {1: "Passed", 7: "N/A", 11: "status_11"}

    def test_the_tests_of_a_run_follow_the_pagination(self, monkeypatch):
        asked = []
        pages = {
            "get_tests/5&limit=250": {"tests": [{"id": 1, "case_id": 10, "title": "a", "status_id": 3}],
                                      "_links": {"next": "/api/v2/get_tests/5&limit=250&offset=250"}},
            "get_tests/5&limit=250&offset=250": {"tests": [{"id": 2, "case_id": 11, "title": None,
                                                            "status_id": 1}],
                                                 "_links": {"next": None}},
        }

        def get(endpoint):
            asked.append(endpoint)
            return pages[endpoint]
        monkeypatch.setattr(mt, "_get", get)
        tests = mt.fetch_run_tests.__wrapped__(5)
        assert [t["case_id"] for t in tests] == [10, 11]
        assert tests[1]["title"] == ""                   # a test without a title is not None
        assert asked == ["get_tests/5&limit=250", "get_tests/5&limit=250&offset=250"]

    def test_an_endless_next_that_points_at_nothing_stops(self, monkeypatch):
        monkeypatch.setattr(mt, "_get", lambda e: {"tests": [], "_links": {"next": "/api/v2/loop"}})
        assert mt.fetch_run_tests.__wrapped__(5) == []

    def test_the_plan_url_names_the_plan(self, monkeypatch):
        monkeypatch.setattr(st, "secrets", {}, raising=False)
        assert mt.plan_url("https://x.example") == "https://x.example/index.php?/plans/view/3721"
