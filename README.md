# 🧪 Automation Coverage

A Streamlit dashboard that connects to **TestRail** and gives a live,
multi-dimensional view of test automation coverage across Business Units,
countries, devices and frameworks — plus run health, flakiness and release
readiness.

---

## What it does

The app pulls test case data directly from the TestRail API and processes it
through a rule engine that understands each BU's specific field names, country
tokens and automation frameworks. Everything is cached (6 h for case data) and
pre-warmed at startup, so after the first load every interaction is instant.

Optional integrations enrich the picture and degrade silently when not
configured: **Jira** (bug status and fix versions on the Runs tab) and
**Dexter**, a Gemini-powered assistant that answers questions about the numbers
using the same cached data the dashboard renders.

### Tabs

| Tab | Purpose |
|---|---|
| **📋 Backlog** | The regression baseline for the selected BU: every `(case × country × device)` row classified into Automated / To update / Backlog / Partially Automated / Not Applicable / Unknown, with per-tile evidence exports, followed by the all-BU summary table |
| **📐 Coverage** | Coverage per functional area (TestRail section), as a pie + bar pair, with drill-down links back into TestRail |
| **🏃 Runs** | Active runs with a stacked result bar and pass %, bugs enriched live from Jira, and a release-readiness card joining the latest completed run with a Jira fix version |
| **📈 Stability** | How dependable the tests are — always-pass / always-fail / flaky classification over the last N runs — plus a deep-dive on a single case's execution history |
| **🧭 Overview** | Cross-BU totals — Smoke Suite, All Automated Cases and Production Sanity — broken down by country and device, over any subset of BUs. These are *automated* counts, not baseline coverage: for that, read the Backlog tab |
| **📄 Report** | Presentation-ready Altair charts (per BU × country × device, plus a coverage leaderboard), suitable for copy-pasting into slides |

A floating chat button (bottom-left, every tab) opens **Dexter**.

---

## Scopes and the global filter

One control bar sits between the header and the tab bar and is the only
scope/BU selector in the app — tabs read it via `global_filter.current()` and
never render their own:

```
[ 🌐 Website · 📱 Mobile App · 🧩 Microservices ]   [ Business Unit ▾ ]
```

Each scope keeps its own last-selected BU, so switching back and forth can never
produce an invalid combination. The selection is published to the URL as
`?scope=…&bu=…`, which makes any view linkable — paste the link and the
recipient lands on exactly what you were looking at.

All-BU sections (Overview, Report, the Backlog summary table) are cross-BU
comparisons by design and intentionally ignore the BU part of the selection.

---

## Architecture

```
app.py                      Streamlit entry point: header, credential gate,
                            KPI strip, global filter, 6 tabs, cache warm-up
                            and the background refresh watchdog
│
├── src/
│   ├── testrail_client.py  TestRail API wrapper — pagination, retries, pacing,
│   │                       parallel prefetch, st.cache_data
│   ├── field_resolver.py   Custom field labels → system names and value ids,
│   │                       with per-project configs
│   ├── bu_rules.py         Rule definitions: one Rule per (BU, framework, scope),
│   │                       country tokens, run-name aliases
│   ├── rules_engine.py     Evaluates rules → raw_cases + automated DataFrames,
│   │                       framework precedence, cache warm-up
│   ├── metrics.py          Aggregation helpers (smoke, totals, prod sanity)
│   ├── methodology.py      Canonical description of how every number is computed
│   ├── jira_client.py      Read-only Jira enrichment (best-effort)
│   └── ui/
│       ├── global_filter.py  Scope + BU selector, shareable via URL
│       ├── kpi_strip.py      Executive KPI row under the header
│       ├── backlog_tab.py    Backlog tab
│       ├── coverage_tab.py   Coverage tab
│       ├── runs_tab.py       Runs tab + the Stability renderers
│       ├── stability_tab.py  Stability tab composition
│       ├── overview_tab.py   Overview tab
│       ├── report_tab.py     Report tab
│       ├── data_quality.py   TestRail hygiene checklist
│       ├── chat_assistant.py Dexter — the Gemini assistant
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
the Coverage tab and the KPI strip always report the same figure for the same BU
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
`Automation Status` **and** carries the `playwright` label. On the four BUs whose
rules don't read that generic field (Kruidvat, Trekpleister, Marionnaud,
Watsons) a dedicated rule gates on the label, and it fails *closed* — a case
whose labels can't be resolved is rejected rather than counted.

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
Case data, sections, labels and the rule evaluation are cached for **6 hours**;
runs, plans and results for **10 minutes**; the custom-field registry for
**15 minutes**; Jira lookups for 5 to 30 minutes depending on the endpoint. On
startup `warmup_cache()` fetches every suite in parallel and then pre-computes
the expansion per scope, so switching tabs is instant. The Mobile App scope is
deferred — it loads the first time someone selects it.

While anyone has the app open, an invisible fragment re-warms the data about
30 minutes before the 6 h TTL expires (single-flight across sessions), so no one
lands on an expired cache and pays the reload interactively. The **↻** next to
the "Updated …" label forces an immediate refresh.

A cold start takes roughly a minute: TestRail Cloud rate-limits at ~180
requests/minute and the full case set paginates into about that many requests.
That ceiling is TestRail's, not the app's.

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
GEMINI_MODEL     = "gemini-2.5-flash"   # omit to use the built-in fallback chain

# Optional — Jira enrichment for the Runs tab
JIRA_URL           = "https://your-site.atlassian.net"
ATLASSIAN_USER     = "your.email@example.com"
ATLASSIAN_API_KEY  = "your_atlassian_token"
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
parsing, the scope/BU state machine, Jira's graceful degradation and the
Report's regression-flag join.

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
| `altair` | Charts in the Coverage and Report tabs |
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
3. **Add its run-name aliases** to `BU_RUN_ALIASES`, so the Runs and Stability
   tabs can associate TestRail runs with the BU
4. Refresh the app — the BU appears automatically in the global filter and in
   every tab (`WEBSITE_BUS` / `MOBILE_APP_BUS` are derived from the rule set)

---

## Project structure notes

- **Shared suites**: some TestRail suites contain cases for multiple BUs. Each BU
  is identified by its own country token (e.g. `WTR_SPR` for Watsons). Cases
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
