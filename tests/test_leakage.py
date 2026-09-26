"""Leakage: production incidents from Jira, per Business Unit.

No network.  What is locked here is what decides the figures a manager reads:
which Jira project counts for which BU (and which are honestly shown as
groups), how an incident is classified, that counts are never silently
truncated, and how an incident is matched to the tests that cover it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import jira_client as jc
from src import leakage as lk

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _row(key, days_ago, project="IPXL20", priority="Medium", components=()):
    created = NOW - timedelta(days=days_ago)
    return {"key": key, "url": f"https://j/browse/{key}", "project": project,
            "group": lk.group_for_project(project), "summary": "s",
            "created": created, "month": created.strftime("%Y-%m"),
            "priority": priority, "status": "Done", "environment": "PROD",
            "components": list(components), "channel": lk.channel(list(components))}


class TestWhichProjectCountsForWhichBu:
    def test_one_project_one_bu(self):
        assert lk.group_for_bu("ICI Paris XL").projects == ("IPXL20",)
        assert lk.group_for_project("MRN20") == "Marionnaud"

    def test_shared_projects_are_declared_groups_not_a_split(self):
        """BU multi is empty on every incident: EE20 and SD20 cannot be divided
        per BU, so each is one group — whichever of its BUs is selected."""
        ee = lk.group_for_bu("Drogas")
        assert ee.shared and ee is lk.group_for_bu("Watsons Turkey") is lk.group_for_bu("Watsons Ukraine")
        sd = lk.group_for_bu("Savers")
        assert sd.shared and sd is lk.group_for_bu("Superdrug") is lk.group_for_bu("Superdrug / Savers")

    def test_a_bu_without_a_project_is_not_given_someone_elses(self):
        assert lk.group_for_bu("Trekpleister") is None
        assert lk.group_for_bu("Microservices") is None

    def test_unmapped_projects_stay_visible_as_other(self):
        assert lk.group_for_project("KVKIDS") == lk.OTHER
        assert lk.group_for_project("INNOVATION") == lk.OTHER

    def test_every_group_names_real_dashboard_bus(self):
        from src.bu_rules import ALL_RULES
        bus = {r.bu for r in ALL_RULES}
        for g in lk.GROUPS:
            assert set(g.bus) <= bus, g.label


class TestClassification:
    def test_app_components_are_app(self):
        assert lk.channel(["Android App"]) == "App"
        assert lk.channel(["iOS app", "Checkout"]) == "App"
        assert lk.channel(["Mobile App"]) == "App"

    def test_other_components_are_web(self):
        assert lk.channel(["Checkout & Order Management"]) == "Web"
        assert lk.channel(["Content Management"]) == "Web"

    def test_no_component_is_not_guessed(self):
        """Kruidvat leaves components empty on 190 of 200 incidents."""
        assert lk.channel([]) == lk.NO_COMPONENT == "No component"

    def test_a_jira_issue_is_normalised(self):
        issue = {"key": "MRN20-9", "fields": {
            "project": {"key": "MRN20"}, "summary": "Checkout down",
            "created": "2026-09-20T08:15:00.000+0200",
            "priority": {"name": "High"}, "status": {"name": "Open"},
            "components": [{"name": "Android app"}],
            "customfield_10500": {"value": "UAT + PROD"}}}
        r = lk.normalise(issue, "https://jira", "customfield_10500")
        assert (r["group"], r["priority"], r["environment"], r["channel"], r["month"]) == (
            "Marionnaud", "High", "UAT + PROD", "App", "2026-09")
        assert r["url"] == "https://jira/browse/MRN20-9"

    def test_an_issue_without_a_creation_date_is_dropped(self):
        assert lk.normalise({"key": "X-1", "fields": {}}, "", None) is None


class TestPeriods:
    def test_windows_do_not_overlap(self):
        rows = [_row("A-1", 10), _row("A-2", 40), _row("A-3", 95)]
        assert [r["key"] for r in lk.window(rows, 30, NOW)] == ["A-1"]
        assert [r["key"] for r in lk.window(rows, 30, NOW, offset=30)] == ["A-2"]
        assert [r["key"] for r in lk.window(rows, 90, NOW)] == ["A-1", "A-2"]

    def test_change_vs_previous_period(self):
        assert lk.change(12, 10) == pytest.approx(20)
        assert lk.change(5, 10) == pytest.approx(-50)

    def test_no_previous_incidents_means_no_percentage(self):
        """+∞% would be arithmetic, not information."""
        assert lk.change(3, 0) is None


class TestLinkToTests:
    def test_references_are_indexed_by_jira_key(self):
        by_key = lk.cases_by_key({1: "IPXL20-7322, SD20-1", 2: "ipxl20-7322", 3: ""})
        assert by_key == {"IPXL20-7322": {1, 2}, "SD20-1": {1}}

    def test_the_three_coverage_outcomes(self):
        by_key = {"A-1": {10}, "A-2": {20, 21}}
        automated = {10}
        assert lk.coverage("A-1", by_key, automated) == lk.COVERED_AUTOMATED
        assert lk.coverage("A-2", by_key, automated) == lk.COVERED_MANUAL
        assert lk.coverage("A-3", by_key, automated) == lk.NOT_LINKED


class TestJiraReadsEverything:
    """A truncated count is a wrong count: the search pages until Jira says
    it is done, and a failure raises instead of reading as zero incidents."""

    class _Resp:
        def __init__(self, payload, ok=True, status=200):
            self._payload, self.ok, self.status_code = payload, ok, status

        def json(self):
            return self._payload

    def test_every_page_is_read(self, monkeypatch):
        pages = [
            {"issues": [{"key": f"A-{i}"} for i in range(100)], "nextPageToken": "t1"},
            {"issues": [{"key": f"B-{i}"} for i in range(40)], "isLast": True},
        ]
        bodies = []

        def _post(url, json, auth, timeout):
            bodies.append(json)
            return self._Resp(pages[len(bodies) - 1])

        monkeypatch.setattr(jc, "_conf", lambda: ("https://j", "u", "t"))
        monkeypatch.setattr(jc.requests, "post", _post)
        issues = jc.search_all("issuetype = x", ("project",))
        assert len(issues) == 140
        assert "nextPageToken" not in bodies[0] and bodies[1]["nextPageToken"] == "t1"

    def test_a_failed_page_raises_rather_than_undercounting(self, monkeypatch):
        monkeypatch.setattr(jc, "_conf", lambda: ("https://j", "u", "t"))
        monkeypatch.setattr(jc.requests, "post",
                            lambda *a, **k: self._Resp({}, ok=False, status=503))
        with pytest.raises(RuntimeError, match="503"):
            jc.search_all("issuetype = x", ("project",))

    def test_not_configured_raises(self, monkeypatch):
        monkeypatch.setattr(jc, "_conf", lambda: None)
        with pytest.raises(RuntimeError):
            jc.search_all("issuetype = x", ("project",))


class TestTab:
    @staticmethod
    def _run(bu):
        from streamlit.testing.v1 import AppTest

        def page(bu):
            from datetime import datetime, timedelta, timezone

            from src import jira_client, leakage
            from src.ui import global_filter, leakage_tab
            now = datetime.now(timezone.utc)

            def row(key, days, project, comp):
                c = now - timedelta(days=days)
                return {"key": key, "url": f"https://j/browse/{key}", "project": project,
                        "group": leakage.group_for_project(project), "summary": "s",
                        "created": c, "month": c.strftime("%Y-%m"), "priority": "High",
                        "status": "Done", "environment": "PROD", "components": comp,
                        "channel": leakage.channel(comp)}

            rows = [row("IPXL20-1", 5, "IPXL20", ["Android App"]),
                    row("IPXL20-2", 50, "IPXL20", []),
                    row("EE20-1", 3, "EE20", ["Checkout"])]
            saved = (jira_client.available, leakage.fetch, global_filter.current,
                     leakage_tab._links)
            jira_client.available = lambda: True
            leakage.fetch = lambda: (rows, False)
            global_filter.current = lambda: ("website", bu)
            leakage_tab._links = lambda: ({"IPXL20-1": {7}}, {7})
            try:
                leakage_tab.render()
            finally:
                (jira_client.available, leakage.fetch, global_filter.current,
                 leakage_tab._links) = saved

        at = AppTest.from_function(page, args=(bu,), default_timeout=30)
        at.run()
        assert not at.exception, at.exception
        return at

    def test_a_mapped_bu_shows_its_incidents_and_the_all_bu_table(self):
        at = self._run("ICI Paris XL")
        html = " ".join(m.value for m in at.markdown)
        assert "Production incidents · ICI Paris XL" in html
        assert "Incidents · last 90 days" in html
        assert "On an automated test" in html
        assert len(at.dataframe) >= 1

    def test_a_shared_group_says_it_cannot_be_split(self):
        at = self._run("Drogas")
        captions = " ".join(c.value for c in at.caption)
        assert "cannot be split per Business Unit" in captions

    def test_a_bu_without_a_project_says_so(self):
        at = self._run("Trekpleister")
        assert any("No Jira project is mapped" in i.value for i in at.info)
