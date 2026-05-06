# 🧪 Automation Coverage

A Streamlit dashboard that connects to **TestRail** and gives a live, multi-dimensional view of test automation coverage across Business Units, countries, devices, and frameworks.

---

## What it does

The app pulls test case data directly from the TestRail API and processes it through a rule engine that understands each BU's specific field names, country tokens, and automation frameworks. Results are cached for one hour so repeated interactions feel instant.

### Tabs

| Tab | Purpose |
|---|---|
| **📊 Explorer** | Interactive pivot over all automated cases — filter by country, device, framework, priority; drill into the test list with direct TestRail links |
| **📋 Backlog** | Regression baseline coverage — cases tagged with `big_regr_desktop` / `big_regr_mobile` labels broken down into Automated / Backlog / Not Applicable |
| **🧭 Overview** | Cross-BU summary table with smoke suite and total automated counts per country |
| **📄 Report** | Presentation-ready chart (Altair) showing automated test counts per BU × country × device, suitable for copy-pasting into slides |
| **🔬 Debug** | Raw field inspection — export the TestRail field registry and case data to diagnose mapping issues |

---

## Architecture

```
app.py                      Streamlit entry point, tab routing, credential gate
│
├── src/
│   ├── testrail_client.py  TestRail API wrapper — pagination, retries, st.cache_data
│   ├── field_resolver.py   Resolves custom field labels → system names and dropdown IDs
│   ├── bu_rules.py         Rule definitions: one Rule per (BU, framework, scope)
│   ├── rules_engine.py     Evaluates rules → produces raw_cases + automated DataFrames
│   ├── metrics.py          Aggregation helpers (smoke, totals, prod sanity)
│   └── ui/
│       ├── pivot_tab.py    Explorer tab
│       ├── backlog_tab.py  Backlog & Coverage tab
│       ├── overview_tab.py Overview tab
│       ├── report_tab.py   Report tab
│       └── debug_tab.py    Debug tab
```

### Key concepts

**Rules** (`bu_rules.py`)  
Each `Rule` object defines:
- Which TestRail suite to read
- Which status field counts as "automated" (e.g. `Automation Status Testim Desktop`)
- Which field holds country tokens (e.g. `multi_countries`)
- Which token values belong to this BU (e.g. `WTR_SPR → Turkey`)
- Which values are considered automated (e.g. `Automated`, `Automated UAT`, ...)

**Expansion** (`rules_engine.py`)  
A single case that covers 3 countries generates 3 rows — one per country. TestIM Desktop and TestIM Mobile are separate rules, so a case automated for both adds a Desktop row *and* a Mobile row. Every `(case_id, country, device)` triple is deduplicated to avoid double-counting.

**Regression Baseline** (`backlog_tab.py`)  
Cases are included in the baseline only if they carry the native TestRail label `big_regr_desktop` and/or `big_regr_mobile`. The label determines the device dimension; country comes from the same country field used by the rules engine. Categories:

| Category | Condition |
|---|---|
| **Automated** | Case appears in the rules engine's automated output |
| **Backlog** | In baseline, not automated, status ≠ *Automation not applicable* |
| **Not Applicable** | Status = *Automation not applicable* in the device-specific field |

**Caching**  
All TestRail API calls go through `@st.cache_data(ttl=3600)`. On startup, `warmup_cache()` pre-fetches all suite data in parallel using `ThreadPoolExecutor` so the first user interaction is fast. Click **🔄 Refresh Numbers** to invalidate all caches and re-fetch.

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

### Configure credentials

Create `.streamlit/secrets.toml`:

```toml
TESTRAIL_URL     = "https://your-instance.testrail.io"
TESTRAIL_USER    = "your.email@example.com"
TESTRAIL_API_KEY = "your_api_key"
```

> **Note:** `.streamlit/secrets.toml` is gitignored by default — never commit credentials.

### Run

```bash
streamlit run app.py
```

The app will open at `http://localhost:8501`.

---

## Deployment (Streamlit Cloud)

1. Push the repo to GitHub (without `secrets.toml`)
2. Create a new app on [share.streamlit.io](https://share.streamlit.io) pointing to `app.py`
3. Add the three secrets (`TESTRAIL_URL`, `TESTRAIL_USER`, `TESTRAIL_API_KEY`) in the Streamlit Cloud secrets panel

---

## Dependencies

| Package | Purpose |
|---|---|
| `streamlit` | UI framework and caching |
| `pandas` | Data manipulation and pivot tables |
| `requests` + `tenacity` | TestRail API calls with retry logic |
| `altair` | Interactive charts in the Report tab |

---

## Adding a new Business Unit

1. **Define the rule** in `src/bu_rules.py` — specify suite ID, country tokens, status field, and framework
2. Add the BU to `WEBSITE_BUS` (or the appropriate scope list)  
3. Refresh the app — the new BU appears automatically in all tabs

For TestIM BUs use the `_testim_pair()` helper which creates both Desktop and Mobile rules in one call. For Java BUs create a single `Rule` with `framework="java"`.

---

## Project structure notes

- **Shared suites**: some TestRail suites contain cases for multiple BUs. Each BU is identified by its own country token (e.g. `WTR_SPR` for Watsons). Cases without a matching token are excluded from all counts for that BU.
- **Native labels**: `big_regr_desktop` / `big_regr_mobile` are native TestRail labels (not custom fields) fetched via `GET get_labels/{project_id}`.
- **Device-specific status**: for TestIM, Desktop and Mobile automation status are tracked in separate fields. The backlog logic classifies each device row independently to avoid misclassification when one device is automated and the other is not.
