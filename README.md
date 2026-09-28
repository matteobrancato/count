# 🧪 Automation Coverage

A Streamlit dashboard that connects to **TestRail** and gives a live,
multi-dimensional view of test automation coverage across Business Units,
countries, devices and frameworks — plus production leakage from Jira and an
AI assistant that answers questions about the numbers.

---

## What it does

The app pulls test case data directly from the TestRail API and processes it
through a rule engine that understands each BU's specific field names, country
tokens and automation frameworks. The data is loaded once a day, by the first
visit, and served from cache for the rest of it — so after that first load every
visit and every interaction is instant.

Optional integrations enrich the picture and degrade silently when not
configured: **Jira** (the Leakage tab) and
**Dexter**, a Gemini-powered assistant that answers questions about the numbers
using the same cached data the dashboard renders.

### Tabs

| Tab | Purpose |
|---|---|
| **📋 Backlog** | Every Business Unit side by side on the chosen run (Big No-Regression, Small No-Regression, Production Sanity): totals, frameworks, outstanding work, Automation save and coverage, with a CSV export |
| **🔎 BU Detail** | The BU picked in the top bar: every `(case × country × device)` row classified into Automated / To update / Backlog / Partially Automated / Not Applicable / Unknown, with per-tile evidence exports, coverage, Automation save, frameworks and the pivot |
| **📐 Coverage** | Coverage per functional area (TestRail section), as a pie + bar pair, with drill-down links back into TestRail |
| **🐞 Leakage** | Defect leakage per release, counted as Delivery's quality report does: pick a fixVersion (released or planned) of the BU's Jira project, and see the leakage ratio and its High/Highest variant against their 25% / 10% limits, the UAT issues and the leaked incidents, each excluded one with its reason. Every leaked incident gets an AI proposal (UAT-detectable or not, a category, the TestRail case that should have caught it, the coverage gap), analysed in the background with live progress; insights by release, category, component and gap; an all-BU view; an Excel export with Jira data and the AI proposals. Jira and Gemini only — no TestRail request |

**🧭 Overview** is a window opened from the utility bar next to the tabs: cross-BU
totals — Smoke Suite, All Automated Cases and Production Sanity — by country and
device, over any subset of BUs. These are *automated* counts, not baseline
coverage: for that, read the Backlog tab.

A floating chat button (bottom-left, every tab) opens **Dexter**.

---

## Scopes and the global filter

Two controls in the top bar, to the right of the title, are the only scope/BU
selector in the app — tabs read them via `global_filter.current()` and never
render their own:

```
[ 🌐 Website | 📱 Mobile App | 🧩 Microservices ]   [ Business Unit ▾ ]
```

Each scope keeps its own last-selected BU, so switching back and forth can never
produce an invalid combination. The selection is published to the URL as
`?scope=…&bu=…`, which makes any view linkable — paste the link and the
recipient lands on exactly what you were looking at.

All-BU sections (Overview, the Backlog and Leakage tables) are cross-BU
comparisons by design and intentionally ignore the BU part of the selection.

---

## Architecture

```
app.py                      Streamlit entry point: credential gate, top bar
                            (title, KPI chips, global filter), 4 tabs, the
                            Overview window, cache warm-up
│
├── src/
│   ├── testrail_client.py  TestRail API wrapper — pagination, retries, pacing,
│   │                       parallel prefetch, st.cache_data
│   ├── field_resolver.py   Custom field labels → system names and value ids,
│   │                       with per-project configs
│   ├── bu_rules.py         Rule definitions: one Rule per (BU, framework, scope),
│   │                       country tokens, BU aliases (Dexter)
│   ├── freshness.py        Once-a-day reload: the "fetched at" stamp, the daily
│   │                       rollover and the one function that clears every cache
│   ├── rules_engine.py     Evaluates rules → raw_cases + automated DataFrames,
│   │                       framework precedence, cache warm-up
│   ├── metrics.py          Aggregation helpers (smoke, totals, prod sanity)
│   ├── methodology.py      Canonical description of how every number is computed
│   ├── jira_client.py      Read-only Jira reads for the Leakage tab (best-effort)
│   ├── gemini_client.py    Gemini client + the model fallback policy Dexter uses
│   ├── leakage.py          Leakage per release: releases, windows, Delivery's rules
│   ├── leakage_match.py    Incident → the TestRail case that should have caught it
│   ├── leakage_ai.py       The AI proposal per incident (Gemini, validated JSON)
│   ├── leakage_store.py    AI proposals + analysis runs (in memory, for now)
│   ├── leakage_export.py   The release's analysis as an Excel workbook
│   ├── automation_save.py  Automation save: coefficient × configurations
│   └── ui/
│       ├── global_filter.py  Scope + BU selector, shareable via URL
│       ├── kpi_strip.py      The two cross-BU KPI chips under the title
│       ├── backlog_tab.py    Backlog tab
│       ├── coverage_tab.py   Coverage tab
│       ├── overview_tab.py   Overview window (utility bar)
│       ├── data_quality.py   TestRail hygiene checklist
│       ├── chat_assistant.py Dexter — the Gemini assistant
│       ├── leakage_tab.py    Leakage tab
│       └── styles.py         Design system (colours, CSS, health thresholds)
│
└── tests/                  Pure-Python regression suite (no API calls)
```

### Key concepts

**Rules** (`bu_rules.py`)
Each `Rule` object defines:
- Which TestRail suite to read
- Which status field counts as "automated" (e.g. `Automation Status Testim Desktop`)
- Which field holds country tokens (e.g. `multi_countries`)
- Which token values belong to this BU (e.g. `WTR_SPR → Turkey`)
- Which values are considered automated (e.g. `Automated`, `Automated UAT`, …)
- Optionally, labels the case must carry (used by the Playwright rules)

`WEBSITE_BUS` and `MOBILE_APP_BUS` are *derived* from the rule set, not
maintained by hand.

**Expansion** (`rules_engine.py`)
A single case that covers 3 countries generates 3 rows — one per country. TestIM
Desktop and TestIM Mobile are separate rules, so a case automated for both adds a
Desktop row *and* a Mobile row. Every `(case_id, country, device)` triple is
deduplicated to avoid double-counting.

Some tokens are **conditional**: `IPXL LU` only counts on Highest-priority cases
(`CONDITIONAL_COUNTRY_TOKENS`). The filter is applied at every expansion site —
the automated set, the baseline, the Coverage denominator — so the numbers can
never disagree with each other.

**The baseline** — what the Backlog tab measures, per scope:

| Scope | What's in the baseline | Device |
|---|---|---|
| 🌐 Website | cases labelled `big_regr_desktop` / `big_regr_mobile` | from the label (Desktop / Mobile) |
| 🧩 Microservices | the same labels on API-type cases | `API` |
| 📱 Mobile App | cases with Priority High or Highest (no label exists yet) | the mobile OS (iOS / Android) |

Each baseline row is classified — first match wins:

| Category | Condition |
|---|---|
| **To update** | any status field reads "To be updated" — the test changed under an existing script, so it is work to do, not coverage. This deliberately beats *Automated* |
| **Automated** | the `(case, country, device)` row is in the rules engine's automated output |
| **Not Applicable** | status is "Automation not applicable" |
| **Backlog** | any other non-automated status, and the case is automated nowhere |
| **Partially Automated** | same, or no status at all, but the case *is* automated in another country / on the other device — only that combination is missing |
| **Unknown** | nothing explains the row and the case is automated nowhere — it means a TestRail field is missing, and the Data-Quality panel lists every one of them |

**Coverage**
One definition everywhere: **automated rows ÷ baseline rows**. The Backlog tab,
the Coverage tab and the KPI chips always report the same figure for the same BU
— a property locked by `tests/test_business_rules.py::TestCoverageAgreesWithBacklog`
rather than left to coincidence. Two variants are shown alongside it, never
instead of it:

- *Coverage vs Automatable* excludes the Not Applicable rows.
- *Coverage excluding Partially Automated* takes the partial gaps out of the
  denominator, and appears only for BUs that have such gaps.

The Coverage tab's **Total** view has no baseline to expand — it spans every case
— so it counts cases and says "Coverage by Case" on the card.

**Frameworks**
Three generations of tooling, oldest to newest: Java → Testim → Playwright. A
case can carry traces of more than one, so every row is attributed to the
**newest** framework covering it. The breakdown therefore sums exactly to
Automated, with no row counted twice.

Playwright has no status field of its own: a Playwright case sets the generic
`Automation Status` **and** carries the `playwright` label. On the BUs whose
other rules don't read that generic field (Kruidvat, Trekpleister, Watsons Turkey,
Watsons Ukraine) a dedicated rule gates on the label; Marionnaud reads the generic
field for both of its frameworks and tells them apart by the `java` and
`playwright` labels, each with its own country-coverage field. Every label gate
fails *closed* — a case whose labels can't be resolved is rejected, not counted.

**Production Sanity**
Cases carrying the `prod_sanity` label, executed only in production. It is a
baseline of its own, counted in rows like the regression one; a case in both is
counted in both, so the two totals are not meant to add up. (It used to be
defined by the "Test Automation PRD Run" checkbox — that field no longer counts.)

**Data quality** (`data_quality.py`)
A hygiene checklist computed from the frames already in cache — zero extra
TestRail calls. It surfaces baseline cases with no country token, cases
attributable to no BU at all, "to be deleted" sections that still hold active
cases, and every Unknown row with the reason it went unknown, downloadable as a
workbook for the clean-up work. It lives behind the **🧹 Data quality** popover
in the utility bar above the tabs, which carries the current finding count.

**Caching and freshness**
The numbers are loaded **once per business day** (Europe/Rome), then kept all
day. The first run of a new day finds numbers stamped on an earlier one, clears
every cache and reloads; every other run that day is a cache hit
(`src/freshness.py`). There is no timed refresh. The **↻** next to the
"Updated …" label forces a reload at any time, for everyone.

The TestRail payloads are persisted to disk, so a restart does not cost a
reload; derived frames are cached in memory for the day. On startup `warmup_cache()`
fetches every suite in parallel and pre-computes the expansion per scope, so
switching tabs is instant. The Mobile App scope is deferred — it loads the first
time someone selects it.

**The reload is bounded by TestRail's API limit, not by the app.** The limit is
per user and has moved a lot (about 180 requests/minute until August 2026, 5
from mid-August, rising again since late September). The client learns the
current figure from TestRail's own 429 responses, pools several accounts
(`TESTRAIL_USER_1` … `_8`) to multiply it, and paces every request so a reload
never turns into a storm of rejections. `TESTRAIL_RATE_LIMIT` in the secrets is
a **ceiling**, not the rate.

**Methodology as a single source**
`src/methodology.py` holds the canonical explanation of every number. It feeds
both the "ℹ️ How numbers are calculated" panel users can open and Dexter's system
instruction, so the explanation can never drift from the answer. **Any change to
a counting rule belongs there too.**

---

## Setup

### Prerequisites

- Python 3.11+
- A TestRail instance with API access enabled
- A TestRail API key

### Install

```bash
git clone <repo-url>
cd count
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

A devcontainer is included, so GitHub Codespaces / VS Code Dev Containers will
install the dependencies and start the app automatically.

### Configure credentials

Create `.streamlit/secrets.toml`:

```toml
# Required
TESTRAIL_URL     = "https://your-instance.testrail.io"
TESTRAIL_USER    = "your.email@example.com"
TESTRAIL_API_KEY = "your_api_key"

# Optional — Dexter, the AI assistant (free key: https://aistudio.google.com/apikey)
GEMINI_API_KEY   = "your_gemini_key"
# GEMINI_MODEL   = "gemini-3.5-flash"   # leave it OUT: when set, Dexter uses
#                  only that model and loses the fallback chain (src/gemini_client.py)

# Optional — Jira: the Leakage tab
JIRA_URL           = "https://your-site.atlassian.net"
ATLASSIAN_USER     = "your.email@example.com"
ATLASSIAN_API_KEY  = "your_atlassian_token"

# Optional — Automation save on the Backlog tab (Big No-Regression): the manual
# effort ONE configuration costs per release cycle, derived by the QA team from
# its confirmed savings.  Business figures: keep them here, not in the repo.
# KEEP THIS TABLE AT THE END OF THE FILE: in TOML every key written after a
# [table] header belongs to that table, so TESTRAIL_* keys below it would vanish.
[automation_coefficient]
unit    = "MD per release cycle"   # shown verbatim
as_of   = "2026-09"                # shown verbatim
default = 0.002                    # optional: BUs without a line of their own
"Drogas" = 0.004                   # one line per Business Unit
```

Only the three `TESTRAIL_*` values are required. Without `GEMINI_API_KEY` the
chat button explains it is unconfigured; without the Atlassian values the Jira
columns simply don't appear. Neither produces an error.

> **Note:** `.streamlit/secrets.toml` is gitignored — never commit credentials.

### Run

```bash
streamlit run app.py
```

The app opens at `http://localhost:8501`.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q          # regression suite
ruff check .       # lint
```

The suite is pure Python — no TestRail or Jira calls, no Streamlit runtime — so
it runs in seconds and is safe to execute before every push.
`tests/test_business_rules.py` locks the counting rules (including the agreement
between the Backlog and Coverage tabs); `tests/test_helpers.py` covers input
parsing, the scope/BU state machine, Jira's graceful degradation and how the
app loads its data; `tests/test_leakage.py` the leakage rules, the TestRail
matching, the AI's answer and the analysis runs.  Run them on the pinned
Streamlit (`requirements.txt`): the widgets use its current API.

Dev tooling is deliberately kept out of `requirements.txt` so it never ships to
Streamlit Cloud.

---

## Deployment (Streamlit Cloud)

1. Push the repo to GitHub (without `secrets.toml`)
2. Create a new app on [share.streamlit.io](https://share.streamlit.io) pointing to `app.py`
3. Add the secrets (at minimum the three `TESTRAIL_*` values) in the Streamlit
   Cloud secrets panel

---

## Dependencies

| Package | Purpose |
|---|---|
| `streamlit` | UI framework and caching |
| `pandas` | Data manipulation and pivot tables |
| `requests` + `tenacity` | TestRail / Jira API calls with retry logic |
| `altair` | Charts in the Coverage and Leakage tabs |
| `google-genai` | Dexter, the Gemini assistant (imported lazily — the app boots without it) |
| `openpyxl` | Two-sheet workbook for the Data-Quality export (falls back to CSV if missing) |

Versions are pinned to what is proven on Streamlit Cloud: bump deliberately,
test, then re-pin.

---

## Adding a new Business Unit

1. **Define the rule** in `src/bu_rules.py` — suite id, country tokens, status
   field, framework. For TestIM BUs use the `_testim_pair()` helper, which
   creates both the Desktop and Mobile rules in one call; for Java BUs create a
   single `Rule` with `framework="java"`
2. **Add its country tokens** to `ALL_COUNTRY_TOKENS` if they are new — this is
   what lets Microservices pick them up without a per-rule change
3. **Set `country_field_label` explicitly.** `_testim_pair()` defaults to
   "Testim Country Coverage", which is wrong for a BU that keeps its country in
   `multi_countries` — and the failure is silent: every TestIM case comes out
   un-automated
4. **Add the BU to the display order** — `_BU_ORDER` in
   `src/ui/global_filter.py` — and give it its short codes in `BU_ALIASES`,
   which Dexter reads to understand "SD" or "WTR". Guard tests fail if either
   is missed
5. Refresh the app — the BU appears in the global filter and in every tab
   (`WEBSITE_BUS` / `MOBILE_APP_BUS` are derived from the rule set)

---

## Project structure notes

- **Shared suites**: some TestRail suites contain cases for multiple BUs. Each BU
  is identified by its own country token (e.g. `WTR_SPR` for Watsons Turkey). Cases
  without a matching token are excluded from that BU's counts — and show up in
  the Data-Quality panel, since a case nobody counts is usually a mistake.
- **Per-project field configs**: the same integer id means different things in
  different TestRail projects — id `3` is `TP` (Trekpleister) in the KV project
  and something else elsewhere — so `field_resolver` resolves values per project.
- **Native labels**: `big_regr_desktop`, `big_regr_mobile`, `playwright` and
  `prod_sanity` are native TestRail labels, not custom fields, fetched via
  `GET get_labels/{project_id}`.
- **Device-specific status**: for TestIM, Desktop and Mobile automation status
  live in separate fields, and the baseline classifies each device row
  independently — so one device being automated never misclassifies the other.
- **Tab isolation**: every tab renders inside its own try/except, so a failure in
  one shows an error in place instead of blanking the tabs after it.
