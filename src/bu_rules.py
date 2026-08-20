from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# "playwright" is never set by a Rule: no TestRail field identifies it.  It is
# assigned after matching, from the `playwright` case label — see
# `rules_engine._apply_framework_precedence`.
Framework = Literal["java", "testim_desktop", "testim_mobile", "mobile_app",
                    "playwright"]
Scope = Literal["website", "mobile_app", "next_gen"]

# --------------------------------------------------------------------- constants
AUTOMATED_JAVA       = ["Automated", "Automated DEV", "Automated UAT"]
AUTOMATED_TESTIM     = ["Automated", "Automated DEV", "Automated UAT", "Automated Prod"]
AUTOMATED_FULL       = ["Automated", "Automated DEV", "Automated UAT", "Automated Prod"]

# Canonical field labels (copy-paste from TestRail Customizations screenshot)
# The TestRail label that marks a Playwright test.  Defined here, next to the
# rules that gate on it, and imported by rules_engine so the matcher and the
# framework precedence can never disagree on its spelling.
PLAYWRIGHT_LABEL = "playwright"

# The TestRail label that marks a Java test.  Only Marionnaud needs it: it
# folded its per-country status fields into the shared "Automation Status", so
# the label is now the ONLY thing separating a Java case from a Playwright one
# on the same field.  Elsewhere a dedicated status field still does that job.
JAVA_LABEL = "java"

# The TestRail label that puts a case in the Production Sanity baseline.
# It replaced the "Test Automation PRD Run" checkbox, which no longer counts.
PROD_SANITY_LABEL = "prod_sanity"

# TestRail checkbox marking the SUBSET of the big_regr baseline that also runs
# in the Small / Release No-Regression run.  A subset, not a baseline of its
# own: every Small NR case is a big_regr case too.
#
# Several spellings, first hit wins: the field is known by its LABEL in the UI
# ("Smaller NR") and by a system name in the API, and narrowing this to one of
# them is what silently emptied the run.  `FieldRegistry.field` tries labels
# then system names, so listing both costs nothing and cannot go quiet again.
SMALL_NR_FIELDS = ("Smaller NR", "small_nr",
                   "custom_small_nr", "custom_smaller_nr")

_TESTIM_DESKTOP_LABEL = "Automation Status Testim Desktop"
_TESTIM_MOBILE_LABEL  = "Automation Status Testim Mobile View"  # NOTE: "View" suffix!

# --------------------------------------------------------------------- global token map
# Single source of truth: every country token that can appear in any TestRail field,
# mapped to its ISO display code.
# Used by cross-BU rules (e.g. Microservices) so they pick up new tokens automatically
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
    # Watsons Turkey
    "WTR": "TR", "WTR_SPR": "TR",
    # Drogas (RU = second Latvia locale, maps to LV)
    "LV": "LV", "LT": "LT", "RU": "LV",
}


# --------------------------------------------------------------------- run/plan name aliases
# BU codes that appear in TestRail run/plan names (case-insensitive).  Used by the
# Runs tab to associate a run with the right BU.
#
# A single alias CAN belong to multiple BUs (e.g. "EE" = Eastern Europe,
# which covers Drogas, Watsons Turkey and Marionnaud's CEE countries) — in that case
# the same run will appear under each of them.
#
# Word-boundary regex matching means "TPS" doesn't accidentally match "TP",
# so we can keep overlapping short codes safely.
BU_RUN_ALIASES: dict[str, list[str]] = {
    "Superdrug":        ["SD"],
    "Savers":           ["SV"],
    "The Perfume Shop": ["TPS"],
    "Kruidvat":         ["KV"],
    "Trekpleister":     ["TKP", "TP"],
    "Watsons Turkey":   ["WTR", "EE"],          # EE = Eastern Europe (shared)
    # Deliberately NOT "EE", and not a bare "UA": "EE" would pull Turkey's,
    # Drogas' and Marionnaud's Eastern-Europe runs in here too, and "UA" is
    # short enough to match unrelated run names.  Narrow on purpose — an alias
    # matching nothing shows an empty Runs tab, one matching too much reports
    # another BU's runs as this one's.
    "Watsons Ukraine":  ["WUA"],
    "ICI Paris XL":     ["IPXL"],
    "Marionnaud":       ["MFR", "MRN", "EE"],   # MRN CEE countries
    "Drogas":           ["DRG", "EE"],          # Baltic — Eastern Europe
    "Microservices":    ["NG", "NEXTGEN"],
}


# ── conditional country tokens ────────────────────────────────────────────────
# A token listed here counts ONLY when the case's Priority label satisfies the
# requirement (substring match, case-insensitive: "Highest" matches "Highest"
# and "4 - Highest" but NOT "High").  Applied at EVERY expansion site — the
# automated set (rules_engine), the regression baseline (backlog_tab), the
# Coverage denominator and the per-country status breakdown — so all numbers stay
# consistent.  Currently: ICI's LU counts only for Highest-priority cases.
CONDITIONAL_COUNTRY_TOKENS: dict[str, str] = {
    "IPXL LU": "Highest",
}


def filter_conditional_tokens(tokens, priority_label) -> list:
    """Drop conditional tokens whose priority requirement isn't met."""
    if not tokens:
        return []
    plab = (priority_label or "").strip().lower()
    return [
        t for t in tokens
        if t not in CONDITIONAL_COUNTRY_TOKENS
        or CONDITIONAL_COUNTRY_TOKENS[t].lower() in plab
    ]


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
    # Optional label gate: the case must carry ALL of these TestRail labels.
    # Used by the Playwright rules, whose status field ("Automation Status") is
    # shared with older automation on some BUs — the label is what tells the two
    # apart, so without this gate the rule would sweep in every legacy case that
    # happens to have the generic field filled.
    labels_filter:       list[str]    = field(default_factory=list)


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
    country_field_label: str = "Testim Country Coverage",
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
    # "IPXL LU" is a CONDITIONAL token (see CONDITIONAL_COUNTRY_TOKENS): it
    # counts ONLY for Highest-priority cases (user decision, Jul 2026).
    # Example: a case tagged NL+BE+LU expands to 2 rows per device normally,
    # 3 rows per device when its Priority is Highest.
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
    # Marionnaud folded its four per-country-group status fields into the ONE
    # shared "Automation Status" (2026-08).  What used to tell the frameworks
    # apart — a field each — is now a LABEL each, and the countries each
    # framework actually covers come from its own coverage field:
    #
    #   Java        → Automation Status + label `java`        + Java Country Coverage
    #   Playwright  → Automation Status + label `playwright`  + Playwright Country Coverage
    #
    # TestIM was never used on this BU, so it has no rule here at all.
    #
    # The label gate is load-bearing and fails CLOSED: both rules read the same
    # field with the same automated values, so without it every Java case would
    # also match the Playwright rule and vice versa.
    #
    # The per-country-group split (MFR vs the seven SPR countries) is gone with
    # the fields that caused it — one field and one country source per framework
    # means one rule per framework, over the union of the tokens.
    # Tokens MAT and MAT_SPR both map to "AT"; dedup on
    # (case_id, country_label, device) ensures a case tagged with both counts once.
    MRN_SUITE = 30784

    # Bare and _SPR tokens both resolve to the same ISO code.
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
    MRN_TOKENS = list(MRN_ALL_LABELS)

    # No `country_fallback_field_label` on either rule.  The coverage field IS
    # the statement of which countries the automation covers; falling back to
    # `multi_countries` would let the baseline's country list stand in for it
    # and report a country as automated on the strength of the case merely
    # being scoped to it.
    rules.append(Rule(
        name="MRN JAVA", bu="Marionnaud", scope="website", framework="java",
        suite_id=MRN_SUITE,
        status_field_label="Automation Status",
        # AUTOMATED_FULL, not AUTOMATED_JAVA: it is now the SAME field as
        # Playwright's, and one value on one field cannot mean automated for
        # one framework and backlog for the other.
        automated_values=list(AUTOMATED_FULL),
        countries_filter=MRN_TOKENS,
        country_labels=dict(MRN_ALL_LABELS),
        labels_filter=[JAVA_LABEL],
        type_filter=[],
        country_field_label="Java Country Coverage",
    ))

    rules.append(Rule(
        name="MRN PLAYWRIGHT", bu="Marionnaud", scope="website", framework="playwright",
        suite_id=MRN_SUITE,
        status_field_label="Automation Status",
        automated_values=list(AUTOMATED_FULL),
        countries_filter=MRN_TOKENS,
        country_labels=dict(MRN_ALL_LABELS),
        labels_filter=[PLAYWRIGHT_LABEL],
        type_filter=[],
        country_field_label="Playwright Country Coverage",
    ))

    # ==================================================================== Superdrug
    # Slide label: "GB"
    SD_SUITE = 9422
    rules.append(Rule(
        name="SD JAVA", bu="Superdrug", scope="website", framework="java",
        suite_id=SD_SUITE,
        status_field_label="Automation Status",
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

    # ==================================================================== Watsons Turkey
    # Suite 7544 is shared across BUs.
    # Token convention is asymmetric across fields:
    #   - Testim Country Coverage : only "WTR"            → drives automated match
    #   - multi_countries         : "WTR" and/or "WTR_SPR" → drives backlog baseline
    # Listing both tokens covers cases that have only WTR_SPR in multi_countries.
    # Both map to slide label "TR" (Turkey).
    WTR_SUITE  = 7544
    WTR_TOKENS = ["WTR", "WTR_SPR"]
    WTR_LABELS = {"WTR": "TR", "WTR_SPR": "TR"}
    rules += _testim_pair("Watsons Turkey", "WTR", WTR_SUITE, WTR_TOKENS,
                          country_labels=WTR_LABELS,
                          implicit_country="TR")

    # ============================================================ Watsons Ukraine
    # A BU in its own right, NOT a country of Watsons Turkey: it has its own
    # suite, so nothing is shared with 7544 and the two never pool.  Country
    # comes from a "UA" token in multi_countries — the same field the baseline
    # is expanded from, so an automated row can never miss its baseline row.
    WUA_SUITE  = 39694
    WUA_TOKENS = ["UA"]
    WUA_LABELS = {"UA": "UA"}
    # `country_field_label` set explicitly: _testim_pair defaults to "Testim
    # Country Coverage", which is right for Watsons Turkey but wrong here —
    # Ukraine carries its country in multi_countries.  Leaving the default
    # would have looked for UA in a field that does not hold it, and every
    # TestIM case would have come out un-automated.  It also puts the automated
    # rows on the same field the baseline expands from, so they cannot miss it.
    rules += _testim_pair("Watsons Ukraine", "WUA", WUA_SUITE, WUA_TOKENS,
                          country_labels=WUA_LABELS,
                          country_field_label="multi_countries")

    # ==================================================================== Drogas
    # Java: "Automation Status DRG" → custom_automation_status_wtctr_spr
    #   DEV/UAT labels differ from other BUs: 8=Automated Dev only, 9=Automated UAT only
    # TestIM: standard TestIM Desktop + Mobile fields + LV/LT in Testim Country Coverage
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

    # ==================================================================== Microservices
    # Type filter: API only.
    # Country: "country_coverage_automation" field (custom_country_coverage_automation).
    # Tokens and ISO codes confirmed from CSV export (microservices.csv).
    # Microservices uses the global token map — no manual update needed when new BUs are added.
    # System name confirmed from TestRail: custom_country_coverage
    NEXTGEN_SUITE = 9570
    rules.append(Rule(
        name="NEXTGEN ALL", bu="Microservices", scope="next_gen", framework="java",
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
        "Watsons Turkey":           9416,
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

    # ================================================================ Playwright
    # A Playwright case is marked with "Automation Status" = automated plus the
    # `playwright` label.  Six BUs already have a rule reading that field, so
    # their Playwright cases land in the automated set on their own and
    # `rules_engine._apply_framework_precedence` relabels them from the label.
    #
    # These three do NOT read it — their automation lives in BU-specific fields
    # (KV SPR / TP / the Testim pair) — so without a rule of their own a clean
    # Playwright case would be classified UNKNOWN: not automated, and not
    # backlog either, because the status looks automated.  Today those BUs only
    # show Playwright rows where the OLD Testim status was left behind, which
    # means the numbers would collapse the moment anyone cleaned it up.
    #
    # Marionnaud used to be the fourth entry here.  It now declares its own
    # Playwright rule above, because it is the only BU whose Playwright
    # countries come from a dedicated coverage field rather than from
    # `multi_countries`.
    #
    # The label gate is what makes this safe: without it the rule would also
    # sweep in legacy cases whose generic field is filled but whose automation
    # this BU never ran.  Country comes from `multi_countries`, the same field
    # their baseline is expanded from, so an automated row can never miss its
    # baseline row.
    for bu, suite_id, tokens, labels_map in (
        ("Kruidvat",     KV_SUITE,  KV_TOKENS,  KV_LABELS),
        ("Trekpleister", KV_SUITE,  TKP_TOKENS, TKP_LABELS),
        ("Watsons Turkey",      WTR_SUITE, WTR_TOKENS, WTR_LABELS),
        ("Watsons Ukraine", WUA_SUITE, WUA_TOKENS, WUA_LABELS),
    ):
        rules.append(Rule(
            name=f"{bu} PLAYWRIGHT", bu=bu, scope="website", framework="playwright",
            suite_id=suite_id,
            status_field_label="Automation Status",
            automated_values=list(AUTOMATED_FULL),
            countries_filter=list(tokens),
            country_labels=dict(labels_map),
            labels_filter=[PLAYWRIGHT_LABEL],
            type_filter=[],
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