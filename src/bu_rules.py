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

# --------------------------------------------------------------------- global token map
# Single source of truth: every country token that can appear in any TestRail field,
# mapped to its ISO display code.
# Used by cross-BU rules (e.g. Next Gen) so they pick up new tokens automatically
# without requiring per-rule changes — just add the token here.
ALL_COUNTRY_TOKENS: dict[str, str] = {
    # Kruidvat / Trekpleister
    "KVBE": "BE", "KVN": "NL", "TP": "NL",
    # ICI Paris XL
    "IPXL NL": "NL", "IPXL BE": "BE", "IPXL LU": "LU",
    # Marionnaud (bare + SPR variants both resolve to the same ISO code)
    "MFR": "FR",
    "MCH": "CH", "MCH_SPR": "CH",
    "MAT": "AT", "MAT_SPR": "AT",
    "MRO": "RO", "MRO_SPR": "RO",
    "MIT": "IT", "MIT_SPR": "IT",
    "MCZ": "CZ", "MCZ_SPR": "CZ",
    "MSK": "SK", "MSK_SPR": "SK",
    "MHU": "HU", "MHU_SPR": "HU",
    # Superdrug / Savers
    "SD": "GB", "SV": "GB",
    # The Perfume Shop
    "TPSGB": "UK", "TPSIE": "IE",
    # Watsons
    "WTR": "TR", "WTR_SPR": "TR",
    # Drogas (RU = second Latvia locale, maps to LV)
    "LV": "LV", "LT": "LT", "RU": "LV",
}


@dataclass(frozen=True)
class Rule:
    name: str
    bu: str
    scope: Scope
    framework: Framework
    suite_id: int
    status_field_label: str
    automated_values:    list[str]    = field(default_factory=lambda: list(AUTOMATED_TESTIM))
    countries_filter:    list[str]    = field(default_factory=list)
    country_labels:      dict[str,str]= field(default_factory=dict)
    implicit_country:    str | None   = None
    priority_filter:     list[str]    = field(default_factory=list)
    type_filter:         list[str]    = field(default_factory=lambda: ["Regression"])
    # Field used to read country tokens — defaults to "multi_countries".
    # Some BUs (e.g. MRN Java) use "Country Validation"; MRN TestIM uses "Testim Country Coverage".
    country_field_label: str          = "multi_countries"
    # Optional fallback field: if the primary country field is empty for a case,
    # try this field instead (e.g. MRN/NextGen TestIM: CC empty → use Country Validation).
    country_fallback_field_label: str | None = None


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
    country_field_label: str = "multi_countries",
    country_fallback_field_label: str | None = None,
) -> list[Rule]:

    if type_filter is None:
        type_filter = []  # No type restriction — big_regr labels define the baseline
    shared = dict(
        bu=bu, scope=scope, suite_id=suite_id,
        automated_values=list(AUTOMATED_TESTIM),
        countries_filter=list(countries),
        country_labels=dict(country_labels or {}),
        implicit_country=implicit_country,
        type_filter=list(type_filter),
        country_field_label=country_field_label,
        country_fallback_field_label=country_fallback_field_label,
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
        # multi_countries is the correct field for Java (default — no override needed)
    ))
    rules += _testim_pair("Kruidvat", "KV", KV_SUITE, KV_TOKENS,
                          country_labels=KV_LABELS,
                          type_filter=[],
                          country_field_label="Testim Country Coverage")

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
                          implicit_country="NL",
                          type_filter=[],
                          country_field_label="Testim Country Coverage")

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
    # BU-specific status fields per country group. No type_filter.
    # Country matching uses dedicated fields (not multi_countries):
    #   Java  → "Country Validation"       (custom_country_validation)
    #   TestIM → "Testim Country Coverage" (custom_case_country_coverage_testim)
    # Tokens MAT and MAT_SPR both map to "AT" — dedup on (case_id, country_label, device)
    # ensures a case tagged with both counts only once.
    MRN_SUITE = 30784

    # Shared label maps: bare and _SPR tokens both resolve to the same ISO code.
    MRN_ALL_LABELS = {
        "MFR": "FR",
        "MCH": "CH", "MCH_SPR": "CH",
        "MAT": "AT", "MAT_SPR": "AT",
        "MRO": "RO", "MRO_SPR": "RO",
        "MIT": "IT", "MIT_SPR": "IT",
        "MCZ": "CZ", "MCZ_SPR": "CZ",
        "MSK": "SK", "MSK_SPR": "SK",
        "MHU": "HU", "MHU_SPR": "HU",
    }

    # France: Automation Status MFR — country from multi_countries (standard field).
    # TestIM Desktop/Mobile — country from Testim Country Coverage.
    MFR_TOKENS = ["MFR"]
    rules.append(Rule(
        name="MFR JAVA", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE,
        status_field_label="Automation Status MFR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=MFR_TOKENS,
        country_labels={k: v for k, v in MRN_ALL_LABELS.items() if k in MFR_TOKENS},
        type_filter=[],
    ))
    rules += _testim_pair("Marionnaud", "MFR", MRN_SUITE, MFR_TOKENS,
                          country_labels={k: v for k, v in MRN_ALL_LABELS.items() if k in MFR_TOKENS},
                          type_filter=[],
                          country_field_label="Testim Country Coverage",
                          country_fallback_field_label="Country Validation")

    # Other 7 MRN countries (CH, AT, RO, IT, CZ, SK, HU).
    # Java:  Automation Status MRN SPR — country from multi_countries (bare + _SPR tokens).
    # TestIM: TestIM Desktop/Mobile    — country from Testim Country Coverage.
    #         Both bare (MAT) and _SPR (MAT_SPR) tokens are accepted; dedup on
    #         (case_id, country_label, device) ensures a case counts once per country.
    MRN_TOKENS = ["MCH", "MAT", "MRO", "MIT", "MCZ", "MSK", "MHU",
                  "MCH_SPR", "MAT_SPR", "MRO_SPR", "MIT_SPR", "MCZ_SPR", "MSK_SPR", "MHU_SPR"]

    rules.append(Rule(
        name="MRN OTHER JAVA", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE,
        status_field_label="Automation Status MRN SPR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=MRN_TOKENS,
        country_labels={k: v for k, v in MRN_ALL_LABELS.items() if k in MRN_TOKENS},
        type_filter=[],
    ))
    rules += _testim_pair("Marionnaud", "MRN OTHER", MRN_SUITE, MRN_TOKENS,
                          country_labels={k: v for k, v in MRN_ALL_LABELS.items() if k in MRN_TOKENS},
                          type_filter=[],
                          country_field_label="Testim Country Coverage",
                          country_fallback_field_label="Country Validation")

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
        status_field_label="Automation Status",   # generic field — TPS cases use the standard status
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=TPS_TOKENS,
        country_labels=TPS_LABELS,
    ))
    rules += _testim_pair("The Perfume Shop", "TPS", TPS_SUITE, TPS_TOKENS,
                          country_labels=TPS_LABELS)

    # ==================================================================== Watsons
    # Suite 7544 is shared across BUs — country token WTR_SPR identifies Watsons TestIM cases.
    # (WTR = legacy token; WTR_SPR = the active SPR token used for TestIM Desktop/Mobile.)
    # Slide label: "TR" (Turkey).
    WTR_SUITE  = 7544
    WTR_TOKENS = ["WTR_SPR"]
    WTR_LABELS = {"WTR_SPR": "TR"}
    rules += _testim_pair("Watsons", "WTR", WTR_SUITE, WTR_TOKENS,
                          country_labels=WTR_LABELS,
                          implicit_country="TR")

    # ==================================================================== Drogas
    # Java: "Automation Status DRG" → custom_automation_status_wtctr_spr
    #   DEV/UAT labels differ from other BUs: 8=Automated Dev only, 9=Automated UAT only
    # TestIM: standard TestIM Desktop + Mobile fields + LV/LT in multi_countries
    # Slide labels: "LT", "LV"
    DRG_SUITE     = 16093
    DRG_TOKENS    = ["LV", "LT", "RU"]          # RU = second Latvia locale
    DRG_LABELS    = {"LV": "LV", "LT": "LT", "RU": "LV"}
    rules.append(Rule(
        name="DRG ALL", bu="Drogas", scope="website", framework="java",
        suite_id=DRG_SUITE,
        status_field_label="Automation Status",   # generic field — DRG cases use the standard status
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=DRG_TOKENS,
        country_labels=DRG_LABELS,
    ))
    rules += _testim_pair("Drogas", "DRG", DRG_SUITE, DRG_TOKENS,
                          country_labels=DRG_LABELS)

    # ==================================================================== Next Gen
    # Type filter: API only.
    # Country: "country_coverage_automation" field (custom_country_coverage_automation).
    # Tokens and ISO codes confirmed from CSV export (microservices.csv).
    # Next Gen uses the global token map — no manual update needed when new BUs are added.
    # System name confirmed from TestRail: custom_country_coverage
    NEXTGEN_SUITE = 9570
    rules.append(Rule(
        name="NEXTGEN ALL", bu="Next Gen", scope="next_gen", framework="java",
        suite_id=NEXTGEN_SUITE,
        status_field_label="Automation Status",
        automated_values=list(AUTOMATED_FULL),
        type_filter=["API"],
        countries_filter=list(ALL_COUNTRY_TOKENS.keys()),
        country_labels=dict(ALL_COUNTRY_TOKENS),
        country_field_label="custom_country_coverage",
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