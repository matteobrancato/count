"""The KPI cards under the top bar.  Their figures are the cross-BU aggregates the
Backlog table is built from; what is tested here is that the redesign shows
exactly those numbers, against the right target, in the right colour — and that
a missing figure hides the strip instead of drawing zeros."""
from __future__ import annotations

import re

import pytest

from src.ui import kpi_strip
from src.ui.styles import BACKLOG_OK_PCT, COLORS, COVERAGE_TARGET


def _render(monkeypatch, kpis):
    drawn = []
    monkeypatch.setattr(kpi_strip, "_kpis", lambda: kpis)
    monkeypatch.setattr(kpi_strip.st, "markdown", lambda html, **kw: drawn.append(html))
    kpi_strip.render()
    return drawn[0] if drawn else None


KPIS = {"per_bu": [{"bu": "Microservices", "pct": 94.7}, {"bu": "Kruidvat", "pct": 70.0},
                   {"bu": "Watsons Ukraine", "pct": 1.1}],
        "baseline": {"total": 25_000, "auto": 19_600, "backlog": 1_050}}


def test_four_cards_in_a_grid(monkeypatch):
    html = _render(monkeypatch, KPIS)
    assert html.startswith("<div class='kpi-grid'>") and html.count("class='kpi-card'") == 4


def test_the_figures_are_the_aggregates_not_new_ones(monkeypatch):
    html = _render(monkeypatch, KPIS)
    assert ">78.4%<" in html                                  # 19,600 / 25,000
    assert "<strong>19,600</strong> of 25,000 baseline rows" in html
    assert ">1,050<" in html and ">4.2%<" in html             # 1,050 / 25,000
    assert ">94.7%<" in html and ">1.1%<" in html
    assert "<strong>Microservices</strong>" in html and "<strong>Watsons Ukraine</strong>" in html


def test_the_ticks_sit_on_the_target_and_the_limit(monkeypatch):
    html = _render(monkeypatch, KPIS)
    ticks = re.findall(r"<b style='left:([\d.]+)%'></b>", html)
    assert ticks == [f"{COVERAGE_TARGET:.1f}", f"{BACKLOG_OK_PCT:.1f}",
                     f"{COVERAGE_TARGET:.1f}", f"{COVERAGE_TARGET:.1f}"]
    assert f"target {COVERAGE_TARGET:.0f}%" in html and f"healthy ≤ {BACKLOG_OK_PCT:.0f}%" in html


def test_colours_follow_the_group_thresholds(monkeypatch):
    html = _render(monkeypatch, KPIS)
    dots = re.findall(r"class='kpi-dot' style='background:(#[0-9A-Fa-f]{6})'", html)
    # 78.4% is under the 80% target, a 4.2% backlog over the 3% limit, 94.7% above,
    # 1.1% far below 60%
    assert dots == [COLORS["warning"], COLORS["warning"], COLORS["success"], COLORS["danger"]]


def test_a_bu_name_cannot_inject_markup(monkeypatch):
    kpis = {**KPIS, "per_bu": [{"bu": "<b>x</b>", "pct": 90.0}, {"bu": "A & B", "pct": 5.0}]}
    html = _render(monkeypatch, kpis)
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html and "A &amp; B" in html


def test_a_meter_never_leaves_its_track():
    assert "width:100.0%" in kpi_strip._meter(140.0, "#000", 80)
    assert "width:0.0%" in kpi_strip._meter(-5.0, "#000", 80)


@pytest.mark.parametrize("kpis", [{"per_bu": [], "baseline": None},
                                  {"per_bu": [{"bu": "A", "pct": 1.0}], "baseline": None},
                                  {"per_bu": [{"bu": "A", "pct": 1.0}],
                                   "baseline": {"total": 0, "auto": 0, "backlog": 0}}])
def test_no_figures_no_strip_rather_than_zeros(monkeypatch, kpis):
    assert _render(monkeypatch, kpis) is None


def test_a_failure_hides_the_strip(monkeypatch):
    def boom():
        raise RuntimeError("no data")
    monkeypatch.setattr(kpi_strip, "_kpis", boom)
    drawn = []
    monkeypatch.setattr(kpi_strip.st, "markdown", lambda html, **kw: drawn.append(html))
    kpi_strip.render()
    assert drawn == []


def test_the_skeleton_has_the_cards_footprint(monkeypatch):
    drawn = []
    monkeypatch.setattr(kpi_strip.st, "markdown", lambda html, **kw: drawn.append(html))
    kpi_strip.render_skeleton()
    assert drawn[0].count("kpi-skeleton-card") == 4 and drawn[0].startswith("<div class='kpi-grid'>")
