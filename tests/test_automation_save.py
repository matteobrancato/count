"""Automation save: a per-configuration coefficient turned into effort figures.

The coefficient is a business number the QA team derives from its confirmed
savings and configures in the secrets; the dashboard only multiplies it by the
configurations each BU has today.  What is locked here: where the number is
read from, that nothing is ever invented when it is missing, that a shared
default is never passed off as a BU's own figure, and that the arithmetic uses
the same row counts (configurations) as the rest of the Backlog tab.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src import automation_save as sv

TABLE = {
    "unit": "MD per release cycle",
    "as_of": "2026-09",
    "Drogas": 0.004,
    "The Perfume Shop": "0.0015",
    "Kruidvat": -5,                 # nonsense → not configured
    "Savers": True,                 # booleans are not numbers
    "Superdrug": 0,                 # zero would claim "saves nothing"
    "mobile_app": {"Drogas": 0.01},
}


class TestConfig:
    def test_a_website_bu_is_read_by_name(self):
        cfg = sv.config_for("Drogas", "website", TABLE)
        assert (cfg.coefficient, cfg.unit, cfg.as_of, cfg.is_default) == (
            0.004, "MD per release cycle", "2026-09", False)

    def test_numbers_written_as_text_are_accepted(self):
        assert sv.config_for("The Perfume Shop", "website", TABLE).coefficient == 0.0015

    def test_another_scope_reads_its_own_table(self):
        assert sv.config_for("Drogas", "mobile_app", TABLE).coefficient == 0.01
        assert sv.config_for("The Perfume Shop", "mobile_app", TABLE).coefficient is None

    def test_a_missing_bu_is_not_configured_never_zero(self):
        assert sv.config_for("Marionnaud", "website", TABLE).coefficient is None

    def test_invalid_values_are_not_configured(self):
        for bu in ("Kruidvat", "Savers", "Superdrug"):
            assert sv.config_for(bu, "website", TABLE).coefficient is None, bu

    def test_reserved_keys_and_scope_tables_are_never_bus(self):
        table = {**TABLE, "default": 0.003}
        for name in ("unit", "as_of", "mobile_app"):
            cfg = sv.config_for(name, "website", table)
            assert cfg.is_default, name          # falls through to the default
        assert sv.config_for("default", "website", table).is_default

    def test_the_default_fills_the_bus_without_their_own_and_says_so(self):
        table = {**TABLE, "default": 0.003}
        own = sv.config_for("Drogas", "website", table)
        assert (own.coefficient, own.is_default) == (0.004, False)
        other = sv.config_for("Marionnaud", "website", table)
        assert (other.coefficient, other.is_default) == (0.003, True)

    def test_the_root_default_never_reaches_another_scope(self):
        """Mobile App counts by priority and OS: a website coefficient is not
        its coefficient.  Only the scope's own table (and its own default)."""
        table = {**TABLE, "default": 0.003}
        assert sv.config_for("Marionnaud", "mobile_app", table).coefficient is None
        table["mobile_app"] = {"default": 0.02}
        assert sv.config_for("Marionnaud", "mobile_app", table).coefficient == 0.02

    def test_the_old_time_save_table_is_not_read(self):
        """The first version stored a TOTAL time save per BU.  Read as a
        coefficient, "ICI Paris XL" = 5 would claim 5 MD per configuration —
        tens of thousands of MD.  The new table has a new name on purpose."""
        assert sv.SECRETS_KEY == "automation_coefficient"

    def test_no_secrets_at_all_is_simply_not_configured(self):
        cfg = sv.config_for("Drogas", "website")      # no secrets file in tests
        assert cfg.coefficient is None and cfg.unit == ""


class TestArithmetic:
    def test_the_formula(self):
        """coefficient × all, × automated and × backlog configurations."""
        s = sv.compute(0.005, automated=1_800, backlog=50, total=2_000)
        assert s.effort == pytest.approx(10.0)
        assert s.saved == pytest.approx(9.0)
        assert s.backlog_gain == pytest.approx(0.25)

    def test_saved_over_effort_is_the_coverage(self):
        """Both are the same coefficient times a row count, so their ratio is
        automated ÷ total — the Coverage the row already shows."""
        s = sv.compute(0.004, automated=1_700, backlog=120, total=2_100)
        assert s.saved / s.effort == pytest.approx(1_700 / 2_100)

    def test_a_coefficient_derived_from_a_confirmed_saving_gives_it_back(self):
        """A BU saving 7 MD per release cycle over 1,730 automated
        configurations: the coefficient, written to four significant digits,
        must give the 7 MD back."""
        s = sv.compute(float(f"{7 / 1_730:.4g}"), automated=1_730, backlog=40, total=1_900)
        assert sv.fmt(s.saved) == "7.0"

    def test_nothing_automated_saves_nothing_without_an_error(self):
        s = sv.compute(0.01, automated=0, backlog=17, total=17)
        assert (s.saved, s.effort) == (0.0, pytest.approx(0.17))
        assert sv.fmt(s.saved) == "0"

    def test_figures_read_like_a_report(self):
        assert sv.fmt(145.2) == "145"
        assert sv.fmt(12_345.6) == "12,346"
        assert sv.fmt(9.0) == "9.0"
        assert sv.fmt(11.24) == "11.2"
        assert sv.fmt(0.218) == "0.22"
        assert sv.fmt(0.03) == "0.03"            # never "0.0" for a real figure
        assert sv.fmt(0) == "0"

    def test_coefficients_keep_three_significant_digits(self):
        assert sv.fmt_coefficient(0.004321) == "0.00432"
        assert sv.fmt_coefficient(0.012345) == "0.0123"
        assert sv.fmt_coefficient(0.25) == "0.250"
        assert sv.fmt_coefficient(12) == "12.0"
        assert "e" not in sv.fmt_coefficient(0.0000412)


class TestOnTheTab:
    @staticmethod
    def _render(cfg):
        from streamlit.testing.v1 import AppTest

        def page(unit, as_of, coefficient, is_default):
            from src import automation_save
            from src.ui import backlog_tab
            original = automation_save.config_for
            automation_save.config_for = (
                lambda bu, scope: automation_save.Config(unit, as_of, coefficient,
                                                         is_default))
            try:
                backlog_tab._automation_save_line(
                    "Drogas", "website",
                    {"automated": 1_800, "backlog": 50, "total": 2_000})
            finally:
                automation_save.config_for = original

        at = AppTest.from_function(
            page, args=(cfg.unit, cfg.as_of, cfg.coefficient, cfg.is_default),
            default_timeout=30)
        at.run()
        assert not at.exception
        return (" ".join(m.value for m in at.markdown)
                + " ".join(c.value for c in at.caption)
                + " HELP: " + " ".join(m.help or "" for m in at.markdown))

    def test_configured_figures_are_shown_with_their_unit_and_date(self):
        text = self._render(sv.Config("MD per release cycle", "2026-09", 0.005))
        line, help_text = text.split(" HELP: ")
        assert "`9.0` saved" in line
        assert "**Manual effort, all tests** `10.0`" in line
        assert "`+0.25`" in line and "50 configurations" in line
        assert "**Per configuration** `0.00500`" in line and "(default)" not in line
        # Unit and date sit in the line's small info popup, not in the line.
        assert "MD per release cycle, as of 2026-09" in help_text
        assert "MD per release cycle" not in line and "—" not in line

    def test_a_default_coefficient_is_labelled_as_such(self):
        text = self._render(sv.Config("MD per release cycle", "", 0.003, True))
        assert "`0.00300` (default)" in text

    def test_missing_figures_say_so_and_show_no_number(self):
        text = self._render(sv.Config("", "", None))
        assert "no coefficient configured for Drogas yet" in text
        assert "`" not in text


class TestTheSummaryTable:
    """The All-BU table carries the same figure, so the placeholder is visible
    before anyone opens a BU."""

    @staticmethod
    def _display() -> pd.DataFrame:
        return pd.DataFrame([
            {"BU": "Drogas", "Scope": "Website", "Total": 2_000, "Automated": 1_800,
             "Backlog": 50, "Coverage %": 90.0},
            {"BU": "Marionnaud", "Scope": "Website", "Total": 5_000, "Automated": 4_000,
             "Backlog": 0, "Coverage %": 80.0},
        ])

    def test_each_row_uses_its_own_counts(self, monkeypatch):
        from src.ui import backlog_tab as bl
        monkeypatch.setattr(bl.automation_save, "_secrets_table",
                            lambda: {"unit": "MD per release cycle", "Drogas": 0.005})
        saves, unit = bl._summary_saves(self._display(), "website")
        assert unit == "MD per release cycle"
        assert saves["Drogas"].saved == pytest.approx(9.0)
        assert saves["Drogas"].effort == pytest.approx(10.0)
        assert saves["Marionnaud"] is None

    def test_the_column_shows_figures_and_the_placeholder(self):
        from src.ui import backlog_tab as bl
        saves = {"Drogas": sv.compute(0.005, 1_800, 50, 2_000), "Marionnaud": None}
        out = bl._summary_table_html(self._display(), ["Total", "Automated", "Backlog"],
                                     saves=saves, save_unit="MD per release cycle")
        assert "Automation save" in out and "MD per release cycle" in out
        assert ">9.0<" in out and "of 10.0" in out
        assert "Not configured" in out

    def test_no_column_outside_the_big_regression(self):
        from src.ui import backlog_tab as bl
        out = bl._summary_table_html(self._display(), ["Total", "Automated", "Backlog"])
        assert "Automation save" not in out
