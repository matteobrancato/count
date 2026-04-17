"""Declarative BU → filter-rule mapping.

Source of truth: EPAM "Smoke and Regression Tests Coverage: Filter" PDF (April 2026)
cross-referenced with the TestRail Customizations screenshots (April 2026).

All `status_field_label` values MUST match the exact Label shown in the
TestRail Customizations UI (case-insensitive match handled by FieldRegistry).

Confirmed system names from screenshots:
  Automation Status             → custom_automation_status        (generic, Dropdown)
  Automation Status ICI         → custom_automation_status_ici    (IPXL Java)
  Automation Status KV SPR      → custom_automation_status_kv_spr
  Automation Status TP          → custom_automation_status_tp     (TKP Java)
  Automation Status TP SPR      → custom_automation_status_tp_spr
  Automation Status MFR         → custom_automation_status_mfr
  Automation Status MRN SPR     → custom_automation_status_mrn_spr
  Automation Status SD          → custom_automation_status_sd
  Automation Status TPS         → custom_automation_status_tps
  Automation Status TPS SPR     → custom_automation_status_tps_spr
  Automation Status SD SPR      → custom_automation_status_sd_spr
  Automation status WTR SPR     → custom_automation_status_wtr_spr
  Automation Status DRG         → custom_automation_status_wlctr_spr  (weird legacy name)
  Automation Status Testim Desktop   → custom_automation_status_testim
  Automation Status Testim Mobile View → custom_automation_status_mobile_view
  Device                        → custom_device                   (Dropdown)
  Deprecated                    → custom_deprecated               (Checkbox → bool)
  Prod Sanity                   → custom_prod_sanity              (Checkbox → bool)
  multi_countries               → custom_multi_countries          (Multi-select)

NOTE: "Automation status MRN" (automation_status_mrn) and
      "Automation status WTR" (automation_status_wtctr) are INACTIVE — not used.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Framework = Literal["java", "testim_desktop", "testim_mobile", "mobile_app"]
Scope = Literal["website", "mobile_app", "next_gen"]

# --------------------------------------------------------------------- constants
AUTOMATED_JAVA       = ["Automated", "Automated DEV", "Automated UAT"]
AUTOMATED_TESTIM     = ["Automated", "Automated DEV", "Automated UAT", "Automated Prod"]
AUTOMATED_FULL       = ["Automated", "Automated DEV", "Automated UAT", "Automated Prod"]

# Canonical field labels (copy-paste from TestRail Customizations screenshot)
_TESTIM_DESKTOP_LABEL = "Automation Status Testim Desktop"
_TESTIM_MOBILE_LABEL  = "Automation Status Testim Mobile View"  # NOTE: "View" suffix!


@dataclass(frozen=True)
class Rule:
    """A single declarative filter rule.

    A case matches when ALL hold:
      - type_id is in type_filter (default: Regression)
      - deprecated == False  (Checkbox field)
      - status field value ∈ automated_values
      - multi_countries intersects countries_filter  (empty = no filter)
      - priority ∈ priority_filter  (empty = any)
    """
    name: str
    bu: str
    scope: Scope
    framework: Framework
    suite_id: int
    status_field_label: str
    automated_values: list[str]     = field(default_factory=lambda: list(AUTOMATED_TESTIM))
    countries_filter:  list[str]    = field(default_factory=list)
    country_labels:    dict[str,str]= field(default_factory=dict)
    implicit_country:  str | None   = None
    priority_filter:   list[str]    = field(default_factory=list)
    type_filter:       list[str]    = field(default_factory=lambda: ["Regression"])


# --------------------------------------------------------------------- helpers
def _testim_pair(
    bu: str,
    name_base: str,
    suite_id: int,
    countries: list[str],
    country_labels: dict[str, str] | None = None,
    implicit_country: str | None = None,
    scope: Scope = "website",
) -> list[Rule]:
    """Return (TestIM Desktop, TestIM Mobile View) rule pair."""
    shared = dict(
        bu=bu, scope=scope, suite_id=suite_id,
        automated_values=list(AUTOMATED_TESTIM),
        countries_filter=list(countries),
        country_labels=dict(country_labels or {}),
        implicit_country=implicit_country,
    )
    return [
        Rule(name=f"{name_base} TESTIM DESKTOP", framework="testim_desktop",
             status_field_label=_TESTIM_DESKTOP_LABEL, **shared),
        Rule(name=f"{name_base} TESTIM MOBILE", framework="testim_mobile",
             status_field_label=_TESTIM_MOBILE_LABEL, **shared),
    ]


# --------------------------------------------------------------------- rule set
def build_rules() -> list[Rule]:
    rules: list[Rule] = []

    # ==================================================================== KV + TKP
    # Shared baseline suite 722.
    KV_SUITE = 722

    # KV Java — uses "Automation Status KV SPR" field (confirmed screenshot)
    rules.append(Rule(
        name="KV JAVA", bu="Kruidvat", scope="website", framework="java",
        suite_id=KV_SUITE,
        status_field_label="Automation Status KV SPR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["KV"],
        country_labels={"KV": "Kruidvat"},
        implicit_country="Kruidvat",
    ))
    rules += _testim_pair("Kruidvat", "KV", KV_SUITE, ["KV"],
                          country_labels={"KV": "Kruidvat"},
                          implicit_country="Kruidvat")

    # TKP Java — "Automation Status TP" field; no multi_countries filter per PDF
    rules.append(Rule(
        name="TKP JAVA", bu="Trekpleister", scope="website", framework="java",
        suite_id=KV_SUITE,
        status_field_label="Automation Status TP",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=[],          # PDF: no country filter for TKP Java
        implicit_country="Trekpleister",
    ))
    rules += _testim_pair("Trekpleister", "TKP", KV_SUITE, ["TP"],
                          country_labels={"TP": "Trekpleister"},
                          implicit_country="Trekpleister")

    # ==================================================================== IPXL
    IPXL_SUITE   = 30122
    IPXL_TOKENS  = ["IPXLBE", "IPXLNL", "IPXLLU"]
    IPXL_LABELS  = {"IPXLBE": "Belgium", "IPXLNL": "Netherlands", "IPXLLU": "Luxembourg"}

    # IPXL Java — "Automation Status ICI" (ICI = ICI Paris XL code, confirmed screenshot)
    rules.append(Rule(
        name="IPXL JAVA", bu="ICI Paris XL", scope="website", framework="java",
        suite_id=IPXL_SUITE,
        status_field_label="Automation Status ICI",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=IPXL_TOKENS,
        country_labels=IPXL_LABELS,
    ))
    rules += _testim_pair("ICI Paris XL", "IPXL", IPXL_SUITE, IPXL_TOKENS,
                          country_labels=IPXL_LABELS)

    # ==================================================================== Marionnaud
    MRN_SUITE = 30784

    # MFR (France) Java — "Automation Status MFR"
    rules.append(Rule(
        name="MFR JAVA", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE,
        status_field_label="Automation Status MFR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["MFR"],
        country_labels={"MFR": "France"},
    ))
    rules += _testim_pair("Marionnaud", "MFR", MRN_SUITE, ["MFR"],
                          country_labels={"MFR": "France"})

    # Other MRN countries — Java uses "Automation Status MRN SPR" + _SPR tokens
    MRN_OTHER_TOKENS = ["MHU_SPR", "MCZ_SPR", "MAT_SPR", "MIT_SPR", "MRO_SPR", "MSK_SPR"]
    MRN_OTHER_LABELS = {
        "MHU_SPR": "Hungary",  "MCZ_SPR": "Czechia", "MAT_SPR": "Austria",
        "MIT_SPR": "Italy",    "MRO_SPR": "Romania",  "MSK_SPR": "Slovakia",
    }
    rules.append(Rule(
        name="MRN OTHER JAVA", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE,
        status_field_label="Automation Status MRN SPR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=MRN_OTHER_TOKENS,
        country_labels=MRN_OTHER_LABELS,
    ))
    rules += _testim_pair("Marionnaud", "MRN OTHER", MRN_SUITE, MRN_OTHER_TOKENS,
                          country_labels=MRN_OTHER_LABELS)

    # ==================================================================== Superdrug
    SD_SUITE = 9422

    # SD Java — "Automation Status SD" (confirmed screenshot)
    rules.append(Rule(
        name="SD JAVA", bu="Superdrug", scope="website", framework="java",
        suite_id=SD_SUITE,
        status_field_label="Automation Status SD",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["SD"],
        country_labels={"SD": "Superdrug"},
        implicit_country="Superdrug",
    ))
    rules += _testim_pair("Superdrug", "SD", SD_SUITE, ["SD"],
                          country_labels={"SD": "Superdrug"},
                          implicit_country="Superdrug")

    # ==================================================================== Savers
    # No BU-specific automation status field visible in screenshots → generic field
    SV_SUITE = 23967
    rules.append(Rule(
        name="SV JAVA", bu="Savers", scope="website", framework="java",
        suite_id=SV_SUITE,
        status_field_label="Automation Status",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["SV"],
        country_labels={"SV": "Savers"},
        implicit_country="Savers",
    ))
    rules += _testim_pair("Savers", "SV", SV_SUITE, ["SV"],
                          country_labels={"SV": "Savers"},
                          implicit_country="Savers")

    # ==================================================================== The Perfume Shop
    TPS_SUITE  = 11833
    TPS_TOKENS = ["TPSUK", "TPSIE"]
    TPS_LABELS = {"TPSUK": "United Kingdom", "TPSIE": "Ireland"}

    # TPS Java — "Automation Status TPS" (confirmed screenshot)
    rules.append(Rule(
        name="TPS JAVA", bu="The Perfume Shop", scope="website", framework="java",
        suite_id=TPS_SUITE,
        status_field_label="Automation Status TPS",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=TPS_TOKENS,
        country_labels=TPS_LABELS,
    ))
    rules += _testim_pair("The Perfume Shop", "TPS", TPS_SUITE, TPS_TOKENS,
                          country_labels=TPS_LABELS)

    # ==================================================================== Watsons
    # WTR Java: None after SPR (per PDF). Only TestIM rules.
    WTR_SUITE = 7544
    rules += _testim_pair("Watsons", "WTR", WTR_SUITE, ["WTR"],
                          country_labels={"WTR": "Watsons"},
                          implicit_country="Watsons")

    # ==================================================================== Drogas
    # "Automation Status DRG" label → system_name custom_automation_status_wlctr_spr
    DRG_SUITE  = 16093
    DRG_TOKENS = ["LV", "LT"]
    DRG_LABELS = {"LV": "Latvia", "LT": "Lithuania"}
    rules.append(Rule(
        name="DRG ALL", bu="Drogas", scope="website", framework="java",
        suite_id=DRG_SUITE,
        status_field_label="Automation Status DRG",
        automated_values=list(AUTOMATED_FULL),
        countries_filter=DRG_TOKENS,
        country_labels=DRG_LABELS,
    ))

    # ==================================================================== Next Gen
    NEXTGEN_SUITE = 9570
    rules.append(Rule(
        name="NEXTGEN ALL", bu="Next Gen", scope="next_gen", framework="java",
        suite_id=NEXTGEN_SUITE,
        status_field_label="Automation Status",
        automated_values=list(AUTOMATED_FULL),
        countries_filter=[],
    ))

    # ==================================================================== Mobile Apps
    # One suite per BU; no country filter (each suite is already BU-specific).
    # Automation tool breakdown done at UI layer via "Automation MAPP Tool" field.
    mobile_app_suites: dict[str, int] = {
        "Drogas":            19110,
        "Watsons":           9416,
        "ICI Paris XL":      1478,
        "The Perfume Shop":  27553,
        "Superdrug / Savers": 10029,
        "Marionnaud":        8470,
        "Kruidvat":          20995,
    }
    for bu, suite_id in mobile_app_suites.items():
        rules.append(Rule(
            name=f"{bu} MOBILE APP", bu=bu, scope="mobile_app", framework="mobile_app",
            suite_id=suite_id,
            status_field_label="Automation Status",
            automated_values=list(AUTOMATED_FULL),
            countries_filter=[],
        ))

    return rules


# --------------------------------------------------------------------- public
ALL_RULES: list[Rule] = build_rules()

WEBSITE_BUS:    list[str] = sorted({r.bu for r in ALL_RULES if r.scope == "website"})
MOBILE_APP_BUS: list[str] = sorted({r.bu for r in ALL_RULES if r.scope == "mobile_app"})


def rules_for_bu(bu: str, scope: Scope | None = None) -> list[Rule]:
    return [r for r in ALL_RULES if r.bu == bu and (scope is None or r.scope == scope)]


def suites_for_bu(bu: str, scope: Scope | None = None) -> list[int]:
    seen: list[int] = []
    for r in rules_for_bu(bu, scope):
        if r.suite_id not in seen:
            seen.append(r.suite_id)
    return seen
