"""Automation save — the manual effort automation saves, per Business Unit.

The coefficient is NOT computed here.  It is a business figure — the manual
effort one configuration costs per release cycle — derived by the QA team from
the savings they have confirmed, and kept in the Streamlit secrets, never in
this public repository.  The dashboard multiplies it by the configurations each
BU has today, so the figures follow the automation as it grows:

    manual effort, all tests  = coefficient × all configurations
    saved by automation       = coefficient × automated configurations
    backlog would add         = coefficient × backlog configurations

A "configuration" is one baseline row — case × country × device — the unit
every other number on the Backlog tab is counted in.  The saving is also split
by the configuration's device (Desktop, Mobile): the same coefficient times
each device's automated configurations, so the two parts add up to the whole.

Secrets (Streamlit Cloud):

    [automation_coefficient]
    unit    = "MD per release cycle"   # shown verbatim
    as_of   = "2026-09"                # shown verbatim
    default = 0.002                    # optional: every BU not listed below
    "Drogas" = 0.004                   # website scope, by BU name

    [automation_coefficient.next_gen]  # optional: another scope, own default
    "Microservices" = 0.003

A root `default` applies to the website scope only: another scope's baseline
is built differently (Mobile App counts by priority and OS), so it takes a
coefficient only from its own table.

A BU with no coefficient says so on the tab: no figure is ever invented, and
none can be typed on the page — a value typed there and screenshotted would be
a saving nobody measured.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

import streamlit as st

SECRETS_KEY = "automation_coefficient"
DEFAULT_KEY = "default"
_RESERVED = {"unit", "as_of", DEFAULT_KEY}
_SCOPES = {"website", "mobile_app", "next_gen"}


@dataclass(frozen=True)
class Config:
    unit: str
    as_of: str
    coefficient: float | None   # None: nothing configured for this BU/scope
    is_default: bool = False    # True: the scope's default, not the BU's own


@dataclass(frozen=True)
class Saving:
    coefficient: float
    effort: float               # coefficient × all configurations
    saved: float                # coefficient × automated configurations
    backlog_gain: float         # coefficient × backlog configurations
    automated: int
    backlog: int
    total: int
    # (device, saved) for each device the BU has configurations on; empty
    # when the split is not known.
    by_device: tuple[tuple[str, float], ...] = field(default=())


def _secrets_table() -> Mapping:
    try:
        table = st.secrets.get(SECRETS_KEY)
    except Exception:                                                   # noqa: BLE001
        return {}
    return table if isinstance(table, Mapping) else {}


def _number(value) -> float | None:
    """A usable coefficient, or None: text that is not a number, a negative
    and zero all read as "not configured" rather than as a saving of 0."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and math.isfinite(number) else None


def config_for(bu: str, scope: str, table: Mapping | None = None) -> Config:
    """The coefficient for one BU in one scope (see module docstring)."""
    table = _secrets_table() if table is None else table
    unit = str(table.get("unit") or "").strip()
    as_of = str(table.get("as_of") or "").strip()
    if scope == "website":
        values = {k: v for k, v in table.items() if k not in _SCOPES}
    else:
        nested = table.get(scope)
        values = nested if isinstance(nested, Mapping) else {}
    own = _number(values.get(bu)) if bu not in _RESERVED else None
    if own is not None:
        return Config(unit, as_of, own)
    default = _number(values.get(DEFAULT_KEY))
    return Config(unit, as_of, default, is_default=default is not None)


def compute(coefficient: float, automated: int, backlog: int, total: int,
            automated_by_device: Mapping[str, int] | None = None) -> Saving:
    """Turn the coefficient into the figures a manager reads.
    `automated_by_device` splits `automated` by device ({"Desktop": n, ...})."""
    return Saving(
        coefficient=coefficient,
        effort=coefficient * total,
        saved=coefficient * automated,
        backlog_gain=coefficient * backlog,
        automated=automated, backlog=backlog, total=total,
        by_device=tuple((d, coefficient * n) for d, n in (automated_by_device or {}).items()),
    )


def fmt(value: float) -> str:
    """An effort as a manager reads it: whole numbers when large, one decimal
    otherwise, never "0.0" for a small but real figure, and a plain "0" for
    nothing at all."""
    if value == 0:
        return "0"
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.1f}"
    return f"{value:.2g}"


def fmt_coefficient(value: float) -> str:
    """Three significant digits, never scientific notation: 0.00485, 0.0185."""
    if value <= 0:
        return "0"
    decimals = max(0, 2 - math.floor(math.log10(value)))
    return f"{value:,.{decimals}f}"
