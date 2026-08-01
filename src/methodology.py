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
| **Not Applicable** | status "Automation not applicable" |
| **Backlog** | a non-automated status **and** the case is automated nowhere — a script to write from scratch |
| **Partially Automated** | the case IS automated in another country or on the other device — only the missing country/device is left. Also covers rows whose status field cannot describe them: the status is per case, the coverage per country, so a case automated in 3 of its 5 countries leaves 2 rows the field says nothing about |
| **Unknown** | no automation status filled in, so we can't say — shown only when it happens, and it means a field is missing in TestRail |

**Coverage** — one definition everywhere: automated **rows** ÷ baseline rows.
The Backlog tab, the Coverage tab and the KPI strip all show the same figure for
the same Business Unit.

* **Coverage vs Automatable** excludes the Not Applicable rows.
* The Coverage tab's **Total** and **Production Sanity** views have no baseline
  row expansion (that is defined on the regression baseline only), so they count
  cases instead — those two say **"Coverage by Case"** on the card so the basis is
  never in doubt.

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

**Frameworks** — the three generations of tooling, oldest to newest: Java,
Testim, then Playwright. A test can carry more than one, so each row is
attributed to the **newest** framework that covers it — the percentages add up
to 100% and no row is counted twice. A Playwright test carries "Automation
Status" = automated **plus** the `playwright` label; Testim and Java have their
own status fields per BU. On the four BUs whose rules do not read the generic
field (Kruidvat, Trekpleister, Marionnaud, Watsons) the label is what tells a
Playwright case from an older one with the same field filled, so a case missing
it is not counted. Mobile App uses its own tooling (the "Automation MAPP Tool"
field).

**Freshness** — numbers refresh automatically every few hours; the "Updated …"
label next to the tabs shows their real age, and the ↻ next to it forces an
immediate refresh (it re-reads TestRail, so it takes a minute).
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
  the Coverage tab and the KPI strip always agree for the same BU.  Never quote
  a case-based percentage as "coverage".
- A "row" is case × country × device: a case automated on Desktop AND Mobile in
  3 countries is 6 rows.  So row counts are larger than unique-case counts, and
  the two must never be mixed in one ratio.
- The only case-based figure is the Coverage tab's Total / Production Sanity
  views, labelled "Coverage by Case" — those subsets have no row expansion.
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
    · N/A           — status "Automation not applicable"
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
- Frameworks, oldest to newest: Java, Testim (Desktop/Mobile), Playwright.
  A test can carry more than one, so each row counts for the NEWEST framework
  covering it (Playwright > Testim > Java) — the three add up to Automated
  exactly, never more.  Playwright comes from the `playwright` case label;
  Java and Testim from their Automation Status fields.  Mobile App uses its
  own tooling.
- A Playwright case needs BOTH "Automation Status" = automated AND the
  `playwright` label.  The label alone never makes a case automated.
""".strip()
