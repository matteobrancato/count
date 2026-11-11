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
| **📋 Backlog** | Every Business Unit side by side on the chosen run (Big No-Regression, Small No-Regression, Smoke — the regression cases with priority Highest —, Production Sanity, Extended Production Sanity — the last with each BU's cases and the Jira bugs they cite): totals, frameworks, outstanding work, Automation save and coverage, with a CSV export. **A click on a Business Unit opens its window** (below) |
| **🌍 Leakage · All BUs** | Every BU's latest release and trend, and the leaks no automated test covers — the candidates for an extended production suite. Jira and Gemini only — no TestRail request |

**The Business Unit window** (`ui/bu_popup.py`) holds everything specific to one BU, in four tabs:

| Tab | Purpose |
|---|---|
| **🔎 Detail** | Every `(case × country × device)` row classified into Automated / To update / Backlog / In Progress / Partially Automated / Not Applicable / Unknown, with per-tile evidence exports (the three framework tiles included), coverage, Automation save, frameworks and the pivot. It has its own run picker, which starts on the run the table is on |
| **📐 Coverage** | Coverage per functional area (TestRail section), as a pie + bar pair, with drill-down links back into TestRail — for the regression baseline, Smoke, Production Sanity, Extended Production Sanity, or the two sanity suites combined |
| **🐞 Leakage** | Which parts of production keep leaking, so they can be automated. Defect leakage per release, counted as Delivery's quality report does: pick a fixVersion (released or planned) of the BU's Jira project, and see the leakage ratio and its High/Highest variant against their 25% / 10% limits, the UAT issues and the excluded incidents behind the counts, and the leaked incidents. Every leaked incident gets an AI proposal (UAT-detectable or not, a category, the TestRail case that should have caught it, the coverage gap), analysed in the background with live progress; an Excel export with Jira data and the AI proposals. Jira and Gemini only — no TestRail request |
| **📈 Trend** | The BU's last six releases: the ratio, the Jira components that leak, and what automation covers of what leaks |

Closing the window (✕, Esc, a click outside) leaves the table where it was; the tab last used stays selected from one BU to the next.

**🧭 Overview** is a window opened from the utility bar next to the tabs: cross-BU
totals — Smoke Suite, All Automated Cases and Production Sanity — by country and
device, over any subset of BUs. These are *automated* counts, not baseline
coverage: for that, read the Backlog tab.

A floating chat button (bottom-left, every tab) opens **Dexter**.

### Martech

A fourth scope, **🎯 Martech**, shows the Testim automation backlog of the MarTech
Connect project. It lives on **another TestRail instance** (aswatson.testrail.io) and
is read from **one plan**, "Testim Automation BACKLOG" (id 3721): each run is a
Business Unit's country, each test a case to automate there, and its status says how
far it has got. There are no labels or fields behind it. The Backlog table lists the
BUs with their tests, how many are merged (TestRail's *Passed*), the backlog (every
other status except N/A) and one column per status that occurs; a click on a BU opens
its runs, statuses and the tests themselves, with an Excel export.

It is offered only once its API key is in the secrets, and it never touches the main
TestRail pipeline: its own session, its own pacing, and nothing is read at start-up —
the plan (one request) when somebody opens the scope, a BU's tests when somebody opens
the BU. Add, above any `[table]` in the secrets:

```toml
MARTECH_TESTRAIL_API_KEY = "..."        # generated on aswatson.testrail.io (My Settings > API Keys)
# Optional - the defaults are shown:
# MARTECH_TESTRAIL_USER = "..."         # falls back to TESTRAIL_USER (same person)
# MARTECH_TESTRAIL_URL  = "https://aswatson.testrail.io"
# MARTECH_PLAN_ID       = 3721
```

`TESTRAIL_API_KEY_MARTECH`, `TESTRAIL_USER_MARTECH` and `TESTRAIL_URL_MARTECH` are read
too, for anyone who follows the main account's naming; the `MARTECH_…` name wins if both exist.

---

## Scopes and the global filter

One control in the top bar, to the right of the title, is the only scope selector
in the app — sections read it via `global_filter.current()` and never render their
own:

```
[ 🌐 Website | 📱 Mobile App | 🧩 Microservices | 🎯 Martech ]
```

The Business Unit is not picked there: it is the one whose row was last clicked in
the Backlog table (`global_filter.select_bu` / `opened`), kept per scope.

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
                            (title, scope switch), 3 tabs, the
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
│   ├── martech.py          Martech: the backlog plan on the second TestRail
│   │                       instance — own session and pacing, status counts → BUs
│   └── ui/
│       ├── global_filter.py  Scope switch + the BU last opened, shareable via URL
│       ├── choice.py         The one pick-one control: a row of radio buttons
│       ├── bu_popup.py       The Business Unit window and the click that opens it
│       ├── martech_tab.py    Martech's table and BU window
│       ├── isolation.py      Keeps one failing section from blanking the rest
│       ├── backlog_tab.py    Backlog tab
│       ├── coverage_tab.py   Coverage tab
│       ├── overview_tab.py   Overview window (utility bar)
│       ├── data_quality.py   TestRail hygiene checklist
│       ├── chat_assistant.py Dexter — the Gemini assistant
│       ├── leakage_tab.py    Leakage, Trend and All BUs views
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
| 📱 Mobile App | cases with Priority High (No Regression) or Highest (Smoke) | iOS and Android, in every market of the BU (case × platform × market) |

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
One definition everywhere: **automated rows ÷ baseline rows**. The Backlog tab
and the Coverage tab always report the same figure for the same BU
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

**The reload is bounded by TestRail's API limit, not by the app.** The limit has
moved a lot (about 180 requests/minute until August 2026, 5 from mid-August, 20
since late September), and it applies to the instance, not to each user: a
second account adds nothing (measured in September and November 2026), so the
app reads one, `TESTRAIL_USER`. The client learns the current figure from
TestRail's own 429 responses and paces every request so a reload never turns
into a storm of rejections. `TESTRAIL_RATE_LIMIT` in the secrets is a
**ceiling**, not the rate.

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
