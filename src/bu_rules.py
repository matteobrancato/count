"""Declarative BU → filter-rule mapping.

Each Rule is a pure description of "how to count a test for this BU/variant".
`rules_engine.py` consumes these rules and produces per-case expansion rows
(country, device, framework) without any BU-specific code.

Source of truth: the `EPAM-Smoke and Regression Tests Coverage: Filter` PDF
(April 2026). The user flagged a few discrepancies with the PDF; where the PDF
and the user disagreed we trust the PDF.

Framework taxonomy:
    - java            : legacy Java + Selenide + Cucumber (status field is BU-specific)
    - testim_desktop  : TestIM desktop automation
    - testim_mobile   : TestIM mobile automation
    - mobile_app      : native mobile app automation (separate suites)

Section taxonomy used for Tab 2 coverage:
    - "website"     : Java / Testim rules on website suites
    - "mobile_app"  : rules on dedicated mobile app suites
    - "next_gen"    : rules on the Next Gen suite
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Framework = Literal["java", "testim_desktop", "testim_mobile", "mobile_app"]
Scope = Literal["website", "mobile_app", "next_gen"]


# --------------------------------------------------------------------- constants
AUTOMATED_JAVA = ["Automated", "Automated DEV", "Automated UAT"]
AUTOMATED_TESTIM = ["Automated", "Automated DEV", "Automated UAT", "Automated Prod"]
# Next Gen / Drogas use the generic Automation Status with Automated Prod included
AUTOMATED_GENERIC_FULL = ["Automated", "Automated DEV", "Automated UAT", "Automated Prod"]


@dataclass(frozen=True)
class Rule:
    """A single declarative filter rule.

    A case "matches" the rule when ALL of these hold:
        - its type_id is in `types` (default: Regression)
        - its deprecated flag equals `deprecated`
        - its value for the custom status field is in `automated_values`
        - (if `countries_filter` set) its multi_countries intersect it
        - (if `priority_filter` set) its priority is in the list
    """
    name: str                       # display/internal name, e.g. "KV JAVA"
    bu: str                         # "Kruidvat"
    scope: Scope                    # "website" | "mobile_app" | "next_gen"
    framework: Framework
    suite_id: int
    status_field_label: str         # custom field label (resolved via FieldRegistry)
    automated_values: list[str] = field(default_factory=lambda: list(AUTOMATED_TESTIM))
    # multi_countries filter (case must contain ANY of these tokens); empty = no filter
    countries_filter: list[str] = field(default_factory=list)
    # Mapping multi_countries token → *reported* country label. If a token appears in
    # countries_filter but not here, it's reported as itself (stripped of suffix).
    # If a rule applies to a single-country BU (e.g. KV), use {"KV": "Kruidvat"}.
    country_labels: dict[str, str] = field(default_factory=dict)
    # If countries_filter is empty but the rule still "represents" one country, put it here:
    implicit_country: str | None = None
    priority_filter: list[str] = field(default_factory=list)  # empty = any priority
    deprecated: str = "No"
    type_filter: list[str] = field(default_factory=lambda: ["Regression"])


# --------------------------------------------------------------------- helpers
def _testim_pair(
    bu: str, name_base: str, suite_id: int, countries: list[str],
    country_labels: dict[str, str] | None = None,
    implicit_country: str | None = None,
    scope: Scope = "website",
) -> list[Rule]:
    """Return the (desktop, mobile) TestIM rule pair for a website BU."""
    kwargs = dict(
        bu=bu, scope=scope, suite_id=suite_id,
        automated_values=list(AUTOMATED_TESTIM),
        countries_filter=list(countries),
        country_labels=dict(country_labels or {}),
        implicit_country=implicit_country,
    )
    return [
        Rule(name=f"{name_base} TESTIM DESKTOP", framework="testim_desktop",
             status_field_label="Automation Status Testim Desktop", **kwargs),
        Rule(name=f"{name_base} TESTIM MOBILE", framework="testim_mobile",
             status_field_label="Automation Status Testim Mobile", **kwargs),
    ]


# --------------------------------------------------------------------- rule set
def build_rules() -> list[Rule]:
    rules: list[Rule] = []

    # ---------------------------------------------------------------- Kruidvat
    # Baseline 722 shared with Trekpleister. KV Java uses "Automation Status SPR".
    KV_SUITE = 722
    rules.append(Rule(
        name="KV JAVA (SPR)", bu="Kruidvat", scope="website", framework="java",
        suite_id=KV_SUITE, status_field_label="Automation Status SPR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["KV"], country_labels={"KV": "Kruidvat"},
        implicit_country="Kruidvat",
    ))
    rules += _testim_pair("Kruidvat", "KV", KV_SUITE, ["KV"],
                          country_labels={"KV": "Kruidvat"},
                          implicit_country="Kruidvat")

    # ------------------------------------------------------------ Trekpleister
    # Same suite (722). TP Java has NO multi_countries filter per the PDF.
    rules.append(Rule(
        name="TKP JAVA", bu="Trekpleister", scope="website", framework="java",
        suite_id=KV_SUITE, status_field_label="Automation Status TP",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=[],  # intentionally empty — PDF does NOT filter multi_countries for TP Java
        implicit_country="Trekpleister",
    ))
    # TestIM for TP: the user said to count Testim tests where multi_countries contains TP.
    rules += _testim_pair("Trekpleister", "TKP", KV_SUITE, ["TP"],
                          country_labels={"TP": "Trekpleister"},
                          implicit_country="Trekpleister")

    # ------------------------------------------------------------- ICI Paris XL
    IPXL_SUITE = 30122
    IPXL_COUNTRIES = ["IPXLBE", "IPXLNL", "IPXLLU"]
    IPXL_LABELS = {"IPXLBE": "Belgium", "IPXLNL": "Netherlands", "IPXLLU": "Luxembourg"}
    rules.append(Rule(
        name="IPXL JAVA", bu="ICI Paris XL", scope="website", framework="java",
        suite_id=IPXL_SUITE, status_field_label="Automation Status",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=IPXL_COUNTRIES, country_labels=IPXL_LABELS,
    ))
    rules += _testim_pair("ICI Paris XL", "IPXL", IPXL_SUITE, IPXL_COUNTRIES,
                          country_labels=IPXL_LABELS)

    # ---------------------------------------------------------------- Marionnaud
    MRN_SUITE = 30784
    # MFR (France) Java + Testim — no priority filter (barrato nel PDF)
    rules.append(Rule(
        name="MFR JAVA", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE, status_field_label="Automation Status MFR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["MFR"], country_labels={"MFR": "France"},
    ))
    rules += _testim_pair("Marionnaud", "MFR", MRN_SUITE, ["MFR"],
                          country_labels={"MFR": "France"})

    # Other MRN countries (SPR variants). Java uses "Automation Status SPR".
    MRN_OTHER_TOKENS = ["MHU_SPR", "MCZ_SPR", "MAT_SPR", "MIT_SPR", "MRO_SPR", "MSK_SPR"]
    MRN_OTHER_LABELS = {
        "MHU_SPR": "Hungary", "MCZ_SPR": "Czechia", "MAT_SPR": "Austria",
        "MIT_SPR": "Italy",   "MRO_SPR": "Romania", "MSK_SPR": "Slovakia",
    }
    rules.append(Rule(
        name="MRN OTHER JAVA (SPR)", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE, status_field_label="Automation Status SPR",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=MRN_OTHER_TOKENS, country_labels=MRN_OTHER_LABELS,
    ))
    rules += _testim_pair("Marionnaud", "MRN OTHER", MRN_SUITE, MRN_OTHER_TOKENS,
                          country_labels=MRN_OTHER_LABELS)

    # -------------------------------------------------------------- Superdrug
    SD_SUITE = 9422
    rules.append(Rule(
        name="SD JAVA", bu="Superdrug", scope="website", framework="java",
        suite_id=SD_SUITE, status_field_label="Automation Status",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["SD"], country_labels={"SD": "Superdrug"},
        implicit_country="Superdrug",
    ))
    rules += _testim_pair("Superdrug", "SD", SD_SUITE, ["SD"],
                          country_labels={"SD": "Superdrug"},
                          implicit_country="Superdrug")

    # ----------------------------------------------------------------- Savers
    SV_SUITE = 23967
    rules.append(Rule(
        name="SV JAVA", bu="Savers", scope="website", framework="java",
        suite_id=SV_SUITE, status_field_label="Automation Status",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=["SV"], country_labels={"SV": "Savers"},
        implicit_country="Savers",
    ))
    rules += _testim_pair("Savers", "SV", SV_SUITE, ["SV"],
                          country_labels={"SV": "Savers"},
                          implicit_country="Savers")

    # ------------------------------------------------------------ The Perfume Shop
    TPS_SUITE = 11833
    TPS_TOKENS = ["TPSUK", "TPSIE"]
    TPS_LABELS = {"TPSUK": "United Kingdom", "TPSIE": "Ireland"}
    rules.append(Rule(
        name="TPS JAVA", bu="The Perfume Shop", scope="website", framework="java",
        suite_id=TPS_SUITE, status_field_label="Automation Status",
        automated_values=list(AUTOMATED_JAVA),
        countries_filter=TPS_TOKENS, country_labels=TPS_LABELS,
    ))
    rules += _testim_pair("The Perfume Shop", "TPS", TPS_SUITE, TPS_TOKENS,
                          country_labels=TPS_LABELS)

    # ---------------------------------------------------------------- Watsons
    WTR_SUITE = 7544
    rules += _testim_pair("Watsons", "WTR", WTR_SUITE, ["WTR"],
                          country_labels={"WTR": "Watsons"},
                          implicit_country="Watsons")

    # ----------------------------------------------------------------- Drogas
    DRG_SUITE = 16093
    DRG_TOKENS = ["LV", "LT"]
    DRG_LABELS = {"LV": "Latvia", "LT": "Lithuania"}
    # Drogas: PDF only shows ONE rule (generic Automation Status, Automated Prod included).
    # We treat it as a "java" framework rule — it's the only automation track for Drogas.
    rules.append(Rule(
        name="DRG ALL", bu="Drogas", scope="website", framework="java",
        suite_id=DRG_SUITE, status_field_label="Automation Status",
        automated_values=list(AUTOMATED_GENERIC_FULL),
        countries_filter=DRG_TOKENS, country_labels=DRG_LABELS,
    ))

    # ---------------------------------------------------------------- Next Gen
    NEXTGEN_SUITE = 9570
    # Next Gen per PDF has no multi_countries filter. We keep an empty filter
    # (counted once per case) and expose "multi_countries" faceting at UI time
    # so the user can break down by country. MRN special-case (expand base+SPR)
    # is handled in rules_engine via `scope == 'next_gen'`.
    rules.append(Rule(
        name="NEXTGEN ALL", bu="Next Gen", scope="next_gen", framework="java",
        suite_id=NEXTGEN_SUITE, status_field_label="Automation Status",
        automated_values=list(AUTOMATED_GENERIC_FULL),
        countries_filter=[],
    ))

    # ---------------------------------------------------------- Mobile Applications
    # Separate suite per BU. Generic Automation Status, all Automated variants count.
    # Distinction per automation tool is exposed via the "Automation Tool" custom field
    # (resolved at runtime; gracefully skipped if the field doesn't exist).
    mobile_app_suites = {
        "Drogas": 19110,
        "Watsons": 9416,
        "ICI Paris XL": 1478,
        "The Perfume Shop": 27553,
        "Superdrug / Savers": 10029,
        "Marionnaud": 8470,
        "Kruidvat": 20995,
    }
    for bu, suite_id in mobile_app_suites.items():
        rules.append(Rule(
            name=f"{bu} MOBILE APP", bu=bu, scope="mobile_app", framework="mobile_app",
            suite_id=suite_id, status_field_label="Automation Status",
            automated_values=list(AUTOMATED_GENERIC_FULL),
            countries_filter=[],
        ))

    return rules


# --------------------------------------------------------------------- public lookup
ALL_RULES: list[Rule] = build_rules()

WEBSITE_BUS: list[str] = sorted({r.bu for r in ALL_RULES if r.scope == "website"})
MOBILE_APP_BUS: list[str] = sorted({r.bu for r in ALL_RULES if r.scope == "mobile_app"})


def rules_for_bu(bu: str, scope: Scope | None = None) -> list[Rule]:
    return [r for r in ALL_RULES if r.bu == bu and (scope is None or r.scope == scope)]


def suites_for_bu(bu: str, scope: Scope | None = None) -> list[int]:
    seen: list[int] = []
    for r in rules_for_bu(bu, scope):
        if r.suite_id not in seen:
            seen.append(r.suite_id)
    return seen
