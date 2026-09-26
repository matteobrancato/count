"""Automation save: a measured time save turned into per-configuration figures.

The time save is a business number the QA team configures; the dashboard only
does the arithmetic.  What is locked here: where the number is read from, that
nothing is ever invented when it is missing, and that the arithmetic uses the
same row counts (configurations) as the rest of the Backlog tab.
"""
from __future__ import annotations

import pytest

from src import automation_save as sv

TABLE = {
    "unit": "hours per regression cycle",
    "as_of": "2026-09",
    "ICI Paris XL": 120,
    "The Perfume Shop": "64",
    "Kruidvat": -5,                 # nonsense → treated as not configured
    "Drogas": True,                 # booleans are not numbers
    "mobile_app": {"ICI Paris XL": 30},
}


class TestConfig:
    def test_a_website_bu_is_read_by_name(self):
        cfg = sv.config_for("ICI Paris XL", "website", TABLE)
        assert (cfg.time_save, cfg.unit, cfg.as_of) == (
            120.0, "hours per regression cycle", "2026-09")

    def test_numbers_written_as_text_are_accepted(self):
        assert sv.config_for("The Perfume Shop", "website", TABLE).time_save == 64.0

    def test_another_scope_reads_its_own_table(self):
        assert sv.config_for("ICI Paris XL", "mobile_app", TABLE).time_save == 30.0
        assert sv.config_for("The Perfume Shop", "mobile_app", TABLE).time_save is None

    def test_a_missing_bu_is_not_configured_never_zero(self):
        """Zero would read as "automation saves nothing" — a claim, not a gap."""
        assert sv.config_for("Superdrug", "website", TABLE).time_save is None

    def test_invalid_values_are_not_configured(self):
        assert sv.config_for("Kruidvat", "website", TABLE).time_save is None
        assert sv.config_for("Drogas", "website", TABLE).time_save is None

    def test_reserved_keys_and_scope_tables_are_never_bus(self):
        assert sv.config_for("unit", "website", TABLE).time_save is None
        assert sv.config_for("mobile_app", "website", TABLE).time_save is None

    def test_no_secrets_at_all_is_simply_not_configured(self):
        cfg = sv.config_for("ICI Paris XL", "website")      # no secrets file in tests
        assert cfg.time_save is None and cfg.unit == ""


class TestArithmetic:
    def test_the_boss_formula(self):
        """time save ÷ automated configurations, times backlog and total."""
        s = sv.compute(120, automated=4_800, backlog=300, total=6_000)
        assert s.per_configuration == pytest.approx(0.025)
        assert s.backlog_gain == pytest.approx(7.5)
        assert s.at_full_automation == pytest.approx(150)

    def test_nothing_automated_means_no_coefficient_not_a_division_error(self):
        s = sv.compute(10, automated=0, backlog=5, total=5)
        assert (s.per_configuration, s.backlog_gain, s.at_full_automation) == (None,) * 3

    def test_figures_read_like_a_report(self):
        assert sv.fmt(145.2) == "145"
        assert sv.fmt(7.5) == "7.50"
        assert sv.fmt(12.34) == "12.3"
        assert sv.fmt(0.025) == "0.025"
        assert sv.fmt(12_345.6) == "12,346"
        assert sv.fmt(None) == "—"


class TestOnTheTab:
    @staticmethod
    def _render(cfg):
        from streamlit.testing.v1 import AppTest

        def page(unit, as_of, time_save):
            from src import automation_save
            from src.ui import backlog_tab
            original = automation_save.config_for
            automation_save.config_for = (
                lambda bu, scope: automation_save.Config(unit, as_of, time_save))
            try:
                backlog_tab._automation_save_line(
                    "ICI Paris XL", "website",
                    {"automated": 4_800, "backlog": 300, "total": 6_000})
            finally:
                automation_save.config_for = original

        at = AppTest.from_function(page, args=(cfg.unit, cfg.as_of, cfg.time_save),
                                   default_timeout=30)
        at.run()
        assert not at.exception
        return " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)

    def test_configured_figures_are_shown_with_their_unit_and_date(self):
        text = self._render(sv.Config("hours per regression cycle", "2026-09", 120.0))
        assert "`120` saved" in text
        assert "`+7.50`" in text and "300 configurations" in text
        assert "`150`" in text and "`0.025`" in text
        assert "hours per regression cycle, as of 2026-09" in text

    def test_missing_figures_say_so_and_show_no_number(self):
        text = self._render(sv.Config("", "", None))
        assert "no time save configured for ICI Paris XL yet" in text
        assert "`" not in text
