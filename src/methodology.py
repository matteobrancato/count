"""Canonical description of HOW every number on the dashboard is calculated.

Single source of truth, used by:
  * the "How the numbers are calculated" panel every user can open (styles it
    as markdown), and
  * Dexter's system instruction (embedded verbatim), so the assistant explains
    the metrics exactly the way the UI does.

Keeping one copy means the explanation can never drift from the answer.
"""
from __future__ import annotations

# Plain-markdown methodology.  Written for a manager, not for an engineer.
METHODOLOGY_MD = """
**Where the data comes from** — everything is read live from TestRail with the
same pipeline for every view, so the tabs always agree with each other.
**Deprecated cases are always excluded.**

**Business Units & countries** — a BU runs in several countries. A case is
attributed to a BU by the country tokens in its country field, so suites shared
between BUs (e.g. Eastern Europe, or Kruidvat/Trekpleister) are split correctly
and no case is counted for a BU it doesn't belong to.

**Expanded rows vs unique cases** — one case can be automated on more than one
device and in more than one country. Each *(case × country × device)* pair is one
**row**, so the row count is larger than the number of distinct cases. Both are
shown: the big number is rows, the small caption is unique cases.

**The baseline** (what the Backlog tab measures) — depends on the scope:

| Scope | What's in the baseline | Device |
|---|---|---|
| 🌐 Website | cases labelled `big_regr_desktop` / `big_regr_mobile` | from the label (Desktop / Mobile) |
| 🧩 Microservices | the same labels, on API-type cases | `API` (an API test has no desktop/mobile) |
| 📱 Mobile App | cases with **Priority High or Highest** (no label exists yet) | the mobile OS (iOS / Android) |

**How each baseline row is classified**

| Category | Meaning |
|---|---|
| **Automated** | status is Automated / Automated DEV / UAT / Prod *and* the row is in the automated set |
| **To update** | status "To be updated" — was automated, needs maintenance |
| **Not Applicable** | status "Automation not applicable". A Testim Desktop or Testim Mobile field decides only its own device: its N/A never makes the other device's row N/A. Where it is filled, it decides that row over the generic field — "Ready to be automated" on Testim Desktop keeps the Desktop row in the work to do even if the generic field says not applicable, and the other way round |
| **Backlog** | a non-automated status **and** the case is automated nowhere — a script to write from scratch |
| **Partially Automated** | the case IS automated in another country or on the other device — only the missing country/device is left. Also covers rows whose status field cannot describe them: the status is per case, the coverage per country, so a case automated in 3 of its 5 countries leaves 2 rows the field says nothing about |
| **Unknown** | no automation status filled in, so we can't say — shown only when it happens, and it means a field is missing in TestRail |

**Coverage** — one definition everywhere: automated **rows** ÷ baseline rows.
The Backlog tab, the Coverage tab and the KPI chips all show the same figure for
the same Business Unit.

* **Coverage vs Automatable** excludes the Not Applicable rows.
* Every Coverage tab view counts rows, exactly like the Backlog tab: the
  regression baseline, Production Sanity, Extended Production Sanity, and the
  two sanity suites combined.

**Coverage excluding Partially Automated** — the same Coverage with the partial
gaps taken out of the baseline: a test automated for NL but not BE is not held
against the BU for BE. The Backlog still counts in full, because a test nobody
ever automated is real missing coverage. Shown only where such gaps exist, and
never in place of Coverage — it answers "how are we doing on what we started",
not "how much of the regression is covered".

**To be Updated beats Automated** — the tool fields (Testim, Playwright, the
BU-specific ones) say whether an automated script exists; "To be updated" is
written when the test itself has changed. A script that no longer matches its
test is work to do, not coverage, so a flagged row counts as To be Updated even
where automation exists. The tile reports how many of those rows already have a
script, so maintenance stays distinguishable from automation to write from
scratch. Coverage vs Automatable is unaffected in its denominator: both
categories were already automatable.

**Health colours** — 🟢 at or above the 80% target · 🟡 60-79% · 🔴 below 60%.
The Backlog is considered healthy while it stays under **3%** of the baseline.

**Production Sanity** — tests carrying the `prod_sanity` label, executed only
in production. It is a baseline of its own, counted separately from the
regression one: a case carrying both labels is counted in both, so the two
totals are not meant to add up. (It used to be defined by the "Test
Automation PRD Run" checkbox; that field no longer counts.)

**Smoke** — the regression baseline's cases with priority **Highest** in
TestRail: a subset of Big No-Regression, counted on the same rows, so its
coverage is the regression coverage of those cases. The Coverage tab breaks
it down by area like the other views.

**Extended Production Sanity** — tests carrying the `ext_prod_sanity` label:
tests found missing for production incidents and automated for production.
Another baseline of its own, built and counted exactly like Production
Sanity; every BU is listed, with 0 where none of its cases carries the label.
Each case is listed with the Jira bugs its references cite. The Coverage
tab also shows the two sanity suites combined, a case carrying both labels
counted once.

**Frameworks** — the three generations of tooling, oldest to newest: Java,
Testim, then Playwright. A test can carry more than one, so each row is
attributed to the **newest** framework that covers it — the percentages add up
to 100% and no row is counted twice. A Playwright test carries "Automation
Status" = automated **plus** the `playwright` label; Testim and Java have their
own status fields per BU. On the four BUs whose rules do not read the generic
field (Kruidvat, Trekpleister, Marionnaud, Watsons Turkey) the label is what tells a
Playwright case from an older one with the same field filled, so a case missing
it is not counted. Mobile App uses its own tooling (the "Automation MAPP Tool"
field).

**Which countries a Testim script covers** — read from "Testim Country
Coverage", not from `multi_countries`: a script can cover fewer countries than
the case is scoped to, and only the countries it names count as automated. One
exception: **Watsons Turkey** has a single country, so when that field is left
blank the case's own `multi_countries` is used instead — unless it also names
another Business Unit's country, in which case the row is not counted.

**Automation save** — an indicative figure. The QA team derives a
**coefficient** from the savings it has confirmed: the manual effort one
**configuration** (baseline row, case × country × device) costs per release
cycle. The dashboard multiplies it by the BU's configurations today — all of
them for the manual effort of the whole regression, the automated ones for the
effort saved, the Backlog ones for what automating the backlog would add — so
the figures follow the automation as it grows. The effort saved is shown split
by the configuration's device, Desktop and Mobile (a Mobile App baseline is all
mobile); the two parts add up to the whole. Unit and date are shown beside
them; a coefficient shared by several BUs is marked "(default)". Shown on the
Big No-Regression baseline only. A BU with no coefficient shows no figure.

**Leakage** — counted per release, the way Delivery's quality report counts
it. A release is a fixVersion of the BU's main release stream (hotfixes are not
releases). **UAT issues** are every Bug with that fixVersion, whatever its
status, and the Defects with that fixVersion created since the previous release
shipped. **Leaked** are the Production Incidents created from the release date
to the next release's date (to date, while the next one has not shipped),
without those Delivery leaves out: cancelled, no root cause yet, a root cause
of Requirement/Documentation, new requirement, non reproducible, expected
behaviour, data issue, duplicate, security issue, not applicable or not a bug,
and app components or labels (on Kruidvat, the incidents owned by the MAPP
Squad too). **Leakage ratio** = leaked ÷ UAT issues, limit
25%; **High & Highest** = leaked High/Highest ÷ all UAT issues, limit 10%.
Every excluded incident and every Defect left out is listed with the reason.
Delivery starts Defects at a UAT start date Jira does not hold, so a ratio can
differ from the report by a ticket or two; tickets edited after a report was
sent change it too. The BU comes from the Jira project: EE20 (Eastern Europe)
and SD20 (Superdrug & Savers) serve several BUs and are shown as groups.

Each leaked incident gets an **AI proposal**: whether UAT could have caught it,
a category (proposed by the dashboard, to be validated by the QA team), a
rationale with the evidence it used, and the TestRail case that should have
covered it — chosen among cases linked to the incident or similar to it, never
presented as coverage below 0.6 confidence. The coverage gap (manual,
automated, no test case) follows that case's status in the Backlog. These are
proposals, labelled as the AI's everywhere they appear, including the Excel
export. **Trend** and **All BUs** sort each leaked incident by what it means
for automation: no test case or a manual test only (not covered: what an
extended automated production suite should add), an automated test that
missed it, not UAT-detectable, unclear, or not analysed yet. A BU's AI figures
are shown only once every leak of the release is analysed.

**Freshness** — the numbers are loaded from TestRail once a day, by the first
visit of the day, and then served from cache for the rest of it, so every
other visit is instant.  The "Updated …" label next to the tabs shows their
real age; the ↻ next to it forces a reload at any time (it re-reads TestRail,
so it takes a few minutes while TestRail limits its API).
""".strip()


# Compact variant embedded in Dexter's system prompt.  Same rules, phrased for a
# model rather than a reader (kept terse to save context tokens).
METHODOLOGY_FOR_LLM = """
- Data is pulled from TestRail; DEPRECATED cases are ALWAYS excluded.
- Backlog vs Partially Automated: a row is BACKLOG only when its case has no
  automated row anywhere; if the case is automated in another country/device the
  row is PARTIALLY AUTOMATED — including when no status field describes it, since
  the status is per case while the coverage is per country.  Neither counts as Automated, so Coverage is the
  same either way — the split only says whether the work is a new script or an
  extension of an existing one.
- "Coverage excluding Partially Automated" = automated ÷ (total − partially),
  shown per BU only where partial gaps exist.  It is NOT the headline Coverage
  and must never be quoted as "the coverage"; always name it in full.
- "To be updated" in ANY status field wins over Automated: the row is To be Updated
  even if a script exists, because the test changed under it.  Never describe
  those rows as "not automated" — they are automated but out of date.
- Coverage % = automated ROWS ÷ baseline ROWS.  ONE definition: the Backlog tab,
  the Coverage tab and the KPI chips always agree for the same BU.  Never quote
  a case-based percentage as "coverage".
- A "row" is case × country × device: a case automated on Desktop AND Mobile in
  3 countries is 6 rows.  So row counts are larger than unique-case counts, and
  the two must never be mixed in one ratio.
- Production Sanity and Extended Production Sanity are counted in ROWS, the
  same basis as the regression
  baseline, so the Coverage tab and the Backlog tab report one number for it.
- Countries: each BU runs in several countries; a case is attributed to a BU by
  the country tokens in its `multi_countries` field.  Suites shared between BUs
  (e.g. Eastern Europe) are split per country.
- The BASELINE depends on scope:
    · Website      → cases labelled `big_regr_desktop` / `big_regr_mobile`;
                     device comes from the label (Desktop / Mobile).
    · Microservices→ same labels on API-type cases; device is always "API".
    · Mobile App   → cases with Priority High or Highest (no label exists);
                     device is the mobile OS (iOS / Android).
  Each (case × country × device) baseline row is classified as one of:
    · Automated     — status Automated / Automated DEV / UAT / Prod
    · To be updated — status "To be updated" (was automated, needs maintenance)
    · N/A           — status "Automation not applicable" (generic field: every
                      device; a Testim Desktop/Mobile field: its own device only).
                      A FILLED Testim Desktop/Mobile field decides its device's
                      row over the generic field, in both directions.
    · Backlog       — any OTHER non-automated status AND the case is automated
                      nowhere (no automated row in any country / device)
    · Partially automated — same statuses, but the case IS automated in another
                      country or device: only that combination is missing
    · Unknown       — no automation status filled in at all
  "Coverage" = Automated ÷ all rows; "Coverage vs Automatable" excludes
  N/A.  A BU's Backlog is considered healthy while it stays under 3% of the total.
- Health colours: 🟢 ≥80% (target) · 🟡 60-79% · 🔴 <60%.
- Production Sanity = cases with the `prod_sanity` label, a SEPARATE baseline
  that may overlap the regression one — a case in both is counted in both,
  so never add the two totals together.
- Smoke = the Big No-Regression rows whose case has priority Highest — a
  subset of the regression baseline, like Small No-Regression.
- Extended Production Sanity = cases with the `ext_prod_sanity` label, another
  separate baseline built the same way; "Production + Extended Sanity" on the
  Coverage tab counts a case carrying both labels once.
- Frameworks, oldest to newest: Java, Testim (Desktop/Mobile), Playwright.
  A test can carry more than one, so each row counts for the NEWEST framework
  covering it (Playwright > Testim > Java) — the three add up to Automated
  exactly, never more.  Playwright comes from the `playwright` case label;
  Java and Testim from their Automation Status fields.  Mobile App uses its
  own tooling.
- A Playwright case needs BOTH "Automation Status" = automated AND the
  `playwright` label.  The label alone never makes a case automated.
- Automation save (indicative) = a configured coefficient (manual effort per
  configuration per release cycle) × the BU's configurations: × all rows =
  manual effort of all tests, × automated rows = effort saved, × Backlog rows
  = what the backlog would add.  Big No-Regression only; never estimate a
  saving for a BU with no coefficient.
- Leakage is per RELEASE (a fixVersion of the main stream, not a hotfix):
  UAT issues = every Bug of the fixVersion + its Defects created since the
  previous release; leaked = Production Incidents from the release date to
  the next release (or to date), minus Delivery's exclusions (cancelled, no
  root cause, non-defect root causes, app components/labels, KV's MAPP Squad).  Ratio = leaked
  ÷ UAT issues (limit 25%); High & Highest = leaked High/Highest ÷ ALL UAT
  issues (limit 10%).  EE20 and SD20 are multi-BU groups — never split them or
  attribute them to a single BU.  The AI's verdicts are proposals: always
  say that a verdict or category comes from the AI.
- Testim rows take their countries from "Testim Country Coverage", NOT from
  multi_countries: only the countries named there count as automated.  Sole
  exception, Watsons Turkey (one country): a BLANK Testim Country Coverage falls
  back to multi_countries, unless multi_countries also names another BU's
  country — then the row is not counted.  On Watsons Turkey the generic
  "Automation Status" automates nothing on its own (only with the playwright
  label); a "To be updated" in it still beats Automated, as everywhere.
""".strip()
