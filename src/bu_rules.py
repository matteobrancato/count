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
  Automation Status MRN         → custom_automation_status_mrn      (MRN Java, other countries)
  Automation Status MRN SPR     → custom_automation_status_mrn_spr
  Automation Status SD          → custom_automation_status_sd
  Automation Status TPS         → custom_automation_status_tps
  Automation Status TPS SPR     → custom_automation_status_tps_spr
  Automation Status SD SPR      → custom_automation_status_sd_spr
  Automation status WTR SPR     → custom_automation_status_wtr_spr
  Automation Status DRG         → custom_automation_status_wlctr_spr  (weird legacy name)
  Automation Status Testim Desktop   → custom_case_automation_status_testim
  Automation Status Testim Mobile View → custom_case_automation_status_mobile_view
  Device                        → custom_device                   (Dropdown)
  Deprecated                    → custom_deprecated               (Checkbox → bool)
  Prod Sanity                   → custom_case_prod_sanity         (Checkbox → bool)
  multi_countries               → custom_multi_countries          (Multi-select)

NOTE: "Automation status WTR" (automation_status_wtctr) is INACTIVE — not used.
      "Automation status MRN" IS used for Java on non-France MRN countries.
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
    type_filter: list[str] | None = None,
) -> list[Rule]:
    """Return (TestIM Desktop, TestIM Mobile View) rule pair.

    *type_filter* defaults to ["Regression"].  Pass [] to skip the type check
    entirely (safer for BUs where cases may not be consistently typed in TestRail).
    """
    if type_filter is None:
        type_filter = ["Regression"]
    shared = dict(
        bu=bu, scope=scope, suite_id=suite_id,
        automated_values=list(AUTOMATED_TESTIM),
        countries_filter=list(countries),
        country_labels=dict(country_labels or {}),
        implicit_country=implicit_country,
        type_filter=list(type_filter),
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
    # KV suite project config (project-specific): 1=KVBE, 2=KVN, 3=TP
    # type=Regression (standard).  Labels: ISO country codes to match reporting slide.
    KV_SUITE   = 722
    KV_TOKENS  = ["KVBE", "KVN"]
    KV_LABELS  = {"KVBE": "BE", "KVN": "NL"}

    rules.append(Rule(
        name="KV JAVA", bu="Kruidvat", scope="website", framework="java",
        suite_id=KV_SUITE,
        status_field_label="Automation Status KV SPR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=KV_TOKENS,
        country_labels=KV_LABELS,
    ))
    rules += _testim_pair("Kruidvat", "KV", KV_SUITE, KV_TOKENS,
                          country_labels=KV_LABELS)

    # TKP cases carry token "TP" (ID=3 in the KV project config).
    TKP_TOKENS = ["TP"]
    TKP_LABELS = {"TP": "NL"}

    rules.append(Rule(
        name="TKP JAVA", bu="Trekpleister", scope="website", framework="java",
        suite_id=KV_SUITE,
        status_field_label="Automation Status TP",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=TKP_TOKENS,
        country_labels=TKP_LABELS,
        implicit_country="NL",
    ))
    rules += _testim_pair("Trekpleister", "TKP", KV_SUITE, TKP_TOKENS,
                          country_labels=TKP_LABELS,
                          implicit_country="NL")

    # ==================================================================== IPXL
    # Uses the generic "Automation Status" field (not "Automation Status ICI").
    # No type_filter — ICI cases are not consistently typed as Regression/Functional.
    IPXL_SUITE   = 30122
    # Global config (28-value): 6=IPXL NL, 7=IPXL BE, 8=IPXL LU  (with spaces!)
    IPXL_TOKENS  = ["IPXL NL", "IPXL BE", "IPXL LU"]
    IPXL_LABELS  = {"IPXL NL": "NL", "IPXL BE": "BE", "IPXL LU": "LU"}

    rules.append(Rule(
        name="IPXL JAVA", bu="ICI Paris XL", scope="website", framework="java",
        suite_id=IPXL_SUITE,
        status_field_label="Automation Status",   # generic field — ICI cases don't use "Automation Status ICI"
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=IPXL_TOKENS,
        country_labels=IPXL_LABELS,
        type_filter=[],   # no type restriction — cases are not typed as Regression in TestRail
    ))
    rules += _testim_pair("ICI Paris XL", "IPXL", IPXL_SUITE, IPXL_TOKENS,
                          country_labels=IPXL_LABELS,
                          type_filter=[])

    # ==================================================================== Marionnaud
    # BU-specific status fields per country group. No type_filter (cases may not be
    # typed Regression/Functional in TestRail — safer to match on status field alone).
    MRN_SUITE = 30784

    # France: "Automation Status MFR" + multi_countries=MFR
    rules.append(Rule(
        name="MFR JAVA", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE,
        status_field_label="Automation Status MFR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["MFR"],
        country_labels={"MFR": "FR"},
        type_filter=[],
    ))
    rules += _testim_pair("Marionnaud", "MFR", MRN_SUITE, ["MFR"],
                          country_labels={"MFR": "FR"},
                          type_filter=[])

    # Other 7 MRN countries (ISO codes on slide: CH, AT, RO, IT, CZ, SK, HU).
    # Java: "Automation Status MRN" + non-SPR tokens in multi_countries.
    # TestIM: TestIM Desktop/Mobile + _SPR tokens in multi_countries.
    # Both token sets map to the same ISO label → dedup on (case_id, country_label, device)
    # collapses Java+TestIM correctly.
    MRN_JAVA_TOKENS = ["MCH", "MAT", "MRO", "MIT", "MCZ", "MSK", "MHU"]
    MRN_JAVA_LABELS = {
        "MCH": "CH", "MAT": "AT", "MRO": "RO",
        "MIT": "IT", "MCZ": "CZ", "MSK": "SK", "MHU": "HU",
    }
    MRN_SPR_TOKENS = ["MCH_SPR", "MAT_SPR", "MRO_SPR", "MIT_SPR", "MCZ_SPR", "MSK_SPR", "MHU_SPR"]
    MRN_SPR_LABELS = {
        "MCH_SPR": "CH", "MAT_SPR": "AT", "MRO_SPR": "RO",
        "MIT_SPR": "IT", "MCZ_SPR": "CZ", "MSK_SPR": "SK", "MHU_SPR": "HU",
    }
    rules.append(Rule(
        name="MRN OTHER JAVA", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE,
        status_field_label="Automation Status MRN",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=MRN_JAVA_TOKENS,
        country_labels=MRN_JAVA_LABELS,
        type_filter=[],
    ))
    rules += _testim_pair("Marionnaud", "MRN OTHER", MRN_SUITE, MRN_SPR_TOKENS,
                          country_labels=MRN_SPR_LABELS,
                          type_filter=[])

    # ==================================================================== Superdrug
    # Slide label: "GB"
    SD_SUITE = 9422
    rules.append(Rule(
        name="SD JAVA", bu="Superdrug", scope="website", framework="java",
        suite_id=SD_SUITE,
        status_field_label="Automation Status SD",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["SD"],
        country_labels={"SD": "GB"},
        implicit_country="GB",
    ))
    rules += _testim_pair("Superdrug", "SD", SD_SUITE, ["SD"],
                          country_labels={"SD": "GB"},
                          implicit_country="GB")

    # ==================================================================== Savers
    # Slide label: "GB"
    SV_SUITE = 23967
    rules.append(Rule(
        name="SV JAVA", bu="Savers", scope="website", framework="java",
        suite_id=SV_SUITE,
        status_field_label="Automation Status",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["SV"],
        country_labels={"SV": "GB"},
        implicit_country="GB",
    ))
    rules += _testim_pair("Savers", "SV", SV_SUITE, ["SV"],
                          country_labels={"SV": "GB"},
                          implicit_country="GB")

    # ==================================================================== The Perfume Shop
    # Slide labels: UK, IE.  Token TPSUK does not exist → use TPSGB.
    TPS_SUITE  = 11833
    TPS_TOKENS = ["TPSGB", "TPSIE"]
    TPS_LABELS = {"TPSGB": "UK", "TPSIE": "IE"}

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
    # TestIM only (no Java SPR).  Dedicated suite, no multi_countries filter.
    # Slide label: "TR" (Turkey).
    WTR_SUITE = 7544
    rules += _testim_pair("Watsons", "WTR", WTR_SUITE, [],
                          country_labels={},
                          implicit_country="TR")

    # ==================================================================== Drogas
    # Java: "Automation Status DRG" → custom_automation_status_wtctr_spr
    #   DEV/UAT labels differ from other BUs: 8=Automated Dev only, 9=Automated UAT only
    # TestIM: standard TestIM Desktop + Mobile fields + LV/LT in multi_countries
    # Slide labels: "LT", "LV"
    DRG_SUITE     = 16093
    DRG_TOKENS    = ["LV", "LT"]
    DRG_LABELS    = {"LV": "LV", "LT": "LT"}
    DRG_AUTOMATED = ["Automated", "Automated Dev only", "Automated UAT only"]
    rules.append(Rule(
        name="DRG ALL", bu="Drogas", scope="website", framework="java",
        suite_id=DRG_SUITE,
        status_field_label="Automation Status DRG",
        automated_values=DRG_AUTOMATED,
        countries_filter=DRG_TOKENS,
        country_labels=DRG_LABELS,
    ))
    rules += _testim_pair("Drogas", "DRG", DRG_SUITE, DRG_TOKENS,
                          country_labels=DRG_LABELS)

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
