"""Automation save — the time automation saves, per Business Unit.

The time save itself is NOT computed here.  It is a business figure the QA team
measures and configures, in the Streamlit secrets and never in this public
repository.  What the dashboard adds is the arithmetic that turns it into the
figures a manager asks for, on the same baseline every other number on the
Backlog tab is counted in:

    per configuration    = time save ÷ automated configurations
    backlog would add    = per configuration × backlog configurations
    at full automation   = per configuration × total configurations

A "configuration" is one baseline row — case × country × device.

Secrets (Streamlit Cloud):

    [automation_time_save]
    unit  = "hours per regression cycle"      # shown verbatim
    as_of = "2026-09"                         # shown verbatim
    "ICI Paris XL" = 120                      # website scope, by BU name
    "The Perfume Shop" = 64

    [automation_time_save.mobile_app]         # optional: another scope
    "ICI Paris XL" = 30

A BU with nothing configured says so on the tab: no figure is ever invented,
and none can be typed on the page — a value typed there and screenshotted
would be a saving nobody measured.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import streamlit as st

SECRETS_KEY = "automation_time_save"
_RESERVED = {"unit", "as_of"}
_SCOPES = {"website", "mobile_app", "next_gen"}


@dataclass(frozen=True)
class Config:
    unit: str
    as_of: str
    time_save: float | None     # None: nothing configured for this BU/scope


@dataclass(frozen=True)
class Saving:
    time_save: float
    per_configuration: float | None   # None when nothing is automated yet
    backlog_gain: float | None
    at_full_automation: float | None
    automated: int
    backlog: int
    total: int


def _secrets_table() -> Mapping:
    try:
        table = st.secrets.get(SECRETS_KEY)
    except Exception:                                                   # noqa: BLE001
        return {}
    return table if isinstance(table, Mapping) else {}


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def config_for(bu: str, scope: str, table: Mapping | None = None) -> Config:
    """The configured time save for one BU in one scope (see module docstring)."""
    table = _secrets_table() if table is None else table
    unit = str(table.get("unit") or "").strip()
    as_of = str(table.get("as_of") or "").strip()
    if scope == "website":
        values = {k: v for k, v in table.items()
                  if k not in _RESERVED and k not in _SCOPES}
    else:
        nested = table.get(scope)
        values = nested if isinstance(nested, Mapping) else {}
    return Config(unit=unit, as_of=as_of, time_save=_number(values.get(bu)))


def compute(time_save: float, automated: int, backlog: int, total: int) -> Saving:
    """Turn a measured time save into the per-configuration figures."""
    per = time_save / automated if automated > 0 else None
    return Saving(
        time_save=time_save,
        per_configuration=per,
        backlog_gain=per * backlog if per is not None else None,
        at_full_automation=per * total if per is not None else None,
        automated=automated, backlog=backlog, total=total,
    )


def fmt(value: float | None) -> str:
    """A figure as a manager reads it: whole numbers when large, never 12 digits."""
    if value is None:
        return "—"
    if value >= 100:
        return f"{value:,.0f}"
    if value >= 10:
        return f"{value:,.1f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:.3g}"
