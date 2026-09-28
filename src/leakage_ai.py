"""The AI half of the Leakage analysis: why each incident escaped, and which
TestRail case should have caught it.

One Gemini call per batch of incidents (gemini_client.ANALYSIS_CHAIN, the
strongest models first), answering in a fixed JSON schema.  What comes back
is a PROPOSAL: the Key QA confirms or changes it, and both are kept (see
leakage_store).  The numbers of the tab never depend on it — the leakage
ratio is counted from Jira alone; the AI only explains the incidents.

The categories below were written for this feature on 2026-09-28 and are
PROPOSED, not the team's official taxonomy (none was supplied): they are the
single list to edit once the QA team validates them.  The Jira root cause
("Root Cause (EU)") is the delivery team's own field and is shown as it is —
the AI reads it as evidence, it never rewrites it.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from . import gemini_client
from .leakage_match import LIKELY, CaseInfo

DETECTABLE = "UAT-detectable"
NOT_DETECTABLE = "Not UAT-detectable"
NEEDS_REVIEW = "Needs review"
VERDICTS = (DETECTABLE, NOT_DETECTABLE, NEEDS_REVIEW)

# (category, verdict it belongs to, what it means) — PROPOSED, see above.
TAXONOMY: tuple[tuple[str, str, str], ...] = (
    ("Missing test case", DETECTABLE,
     "No test covers the failing scenario, though it could have run in UAT."),
    ("Test not in regression scope", DETECTABLE,
     "A test exists but was not part of the release's regression run."),
    ("Outdated test case", DETECTABLE,
     "A test exists but its steps or expected results no longer match the product."),
    ("Manual execution miss", DETECTABLE,
     "A manual test covers it and was in scope, yet the failure slipped through."),
    ("Automated test miss", DETECTABLE,
     "An automated test covers the scenario but did not catch it (assertions, data, flow)."),
    ("Test data gap", DETECTABLE,
     "Reproducible in UAT with the right data, which the tests did not use."),
    ("Requirement gap", DETECTABLE,
     "Missing or ambiguous requirement, so no test expected this behaviour."),
    ("Browser or device coverage", DETECTABLE,
     "Only on a browser, device or viewport the tests did not cover."),
    ("Production-only configuration or content", NOT_DETECTABLE,
     "Configuration, CMS content or a specific promotion that existed only in production."),
    ("Production-only third party", NOT_DETECTABLE,
     "A payment provider, integration or service that behaves differently only in production."),
    ("Production data", NOT_DETECTABLE,
     "Specific production orders, customers or catalogue data."),
    ("Infrastructure or environment", NOT_DETECTABLE,
     "Outage, performance, deployment or other environment-specific behaviour."),
    ("Needs more information", NEEDS_REVIEW,
     "The ticket does not say enough to decide."),
)
CATEGORIES = tuple(c for c, _v, _m in TAXONOMY)
VERDICT_OF = {c: v for c, v, _m in TAXONOMY}

BATCH = 6
_TEXT_LIMITS = {"description": 1500, "steps": 800, "actual": 600, "expected": 600}


def system_instruction() -> str:
    cats = "\n".join(f"   - {c} ({v}): {m}" for c, v, m in TAXONOMY)
    return f"""You are the QA leakage analyst for A.S. Watson's e-commerce sites.
For each production incident of a release you decide whether it could
reasonably have been caught in UAT, why it escaped, and which existing TestRail
test case should have covered it.  A QA lead will check every answer against
the ticket, so be precise and never invent facts that are not in the ticket.

1. uat_detectability
   - "{DETECTABLE}": the failing behaviour could have been reproduced in UAT
     with the product as released — a real testing gap.
   - "{NOT_DETECTABLE}": it depended on a production-only condition —
     configuration, CMS content or a specific promotion that existed only in
     production; a payment provider, third party or service behaving
     differently only in production; specific production orders, customers or
     data; infrastructure, outages, performance or environment behaviour.
   - "{NEEDS_REVIEW}": the ticket does not say enough to decide.
   Never choose "{NOT_DETECTABLE}" merely because the incident happened in
   production — every incident did.  A production-only promotion means that
   specific promotion was configured only in production; it does NOT make the
   promotions functionality untestable in UAT.  In doubt, prefer
   "{NEEDS_REVIEW}" with a lower confidence.
2. category — exactly one of these, consistent with uat_detectability:
{cats}
3. The Jira "Root Cause (EU)" was set by the delivery team after analysing the
   incident: strong evidence ("Configuration" and "3rd party issue" often point
   to production-only conditions, "Code issue" usually to a testing gap), but
   decide from the whole ticket.
4. testrail_matches — from the TestRail cases listed with the incident, choose
   up to 3 that exercise the failing functionality, each with a confidence
   (0-1) and a reason naming the shared functionality.  Use only case ids from
   the list.  Cases marked LINKED are already known to relate: keep them only
   if they test the failing functionality.  An empty list is the right answer
   when none fits — never force a match.
5. functional_area — short "Area > Sub-area", e.g. "Checkout > Payment".
6. confidence — in uat_detectability and category together, 0 to 1.  Below
   {LIKELY} means a person must decide.
7. rationale — one to three sentences a QA lead can verify against the ticket.
8. evidence — up to 3 items: the field you relied on, a short quote of its
   value, and why it matters.
9. missing_information — the fields that would have allowed a surer decision
   (for example "Steps to Reproduce", "Expected Results", "Environment").
10. corrective_actions — up to 3 concrete actions, for example "Create a
   TestRail case for …", "Add C123 to the regression run", "Update C123's
   expected result …", "Automate C123", "Review the automated test of C123:
   assertions and test data", "Prepare production-like data for … in UAT".
   Never recommend reviewing a whole suite.
Answer in English, one verdict per incident, with the incident key."""


def response_schema():
    """The answer's shape, as a pydantic model the SDK turns into a schema."""
    from pydantic import BaseModel

    category_type = Literal.__getitem__(CATEGORIES)
    verdict_type = Literal.__getitem__(VERDICTS)

    class Evidence(BaseModel):
        field: str
        value: str
        reason: str

    class Match(BaseModel):
        case_id: int
        confidence: float
        reason: str

    class Verdict(BaseModel):
        key: str
        uat_detectability: verdict_type
        category: category_type
        functional_area: str
        confidence: float
        rationale: str
        evidence: list[Evidence]
        missing_information: list[str]
        corrective_actions: list[str]
        testrail_matches: list[Match]

    class Answer(BaseModel):
        verdicts: list[Verdict]

    return Answer


# ── the request ──────────────────────────────────────────────────────────────
def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


def _case_line(case: CaseInfo, tag: str = "") -> str:
    where = f"{case.section} — " if case.section else ""
    return f"- C{case.case_id} [{case.status}]{tag} {where}{case.title}"


def incident_block(row: dict, linked: list[tuple[CaseInfo, str]],
                   candidates: list[CaseInfo]) -> str:
    lines = [
        f"### {row['key']}",
        f"Summary: {row.get('summary', '')}",
        "Priority: {p} | Components: {c} | Labels: {lb} | Environment: {e} | "
        "Root Cause (EU): {rc} | Status: {s}".format(
            p=row.get("priority", ""), c=", ".join(row.get("components", [])) or "none",
            lb=", ".join(row.get("labels", [])) or "none",
            e=row.get("environment") or "not set", rc=row.get("root_cause") or "not set",
            s=row.get("status", "")),
    ]
    for name, label in (("description", "Description"), ("steps", "Steps to Reproduce"),
                        ("actual", "Actual Results"), ("expected", "Expected Results")):
        lines.append(f"{label}: {_clip(row.get(name, ''), _TEXT_LIMITS[name]) or 'not filled'}")
    if row.get("links"):
        lines.append("Linked issues: " + "; ".join(
            f"{lk['key']} ({lk['relation']}, {lk['type']}): {lk['summary']}"
            for lk in row["links"][:6]))
    lines.append("TestRail cases:")
    for case, why in linked:
        lines.append(_case_line(case, f" LINKED ({why})"))
    lines.extend(_case_line(c) for c in candidates)
    if not linked and not candidates:
        lines.append("- none found")
    return "\n".join(lines)


def input_hash(row: dict, linked: list[tuple[CaseInfo, str]],
               candidates: list[CaseInfo]) -> str:
    """Changes when what the AI would read changes — the ticket's text or the
    cases offered — so a stale proposal can be told from a current one."""
    return hashlib.sha1(incident_block(row, linked, candidates).encode()).hexdigest()[:16]


# ── the answer ───────────────────────────────────────────────────────────────
def _json_payload(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(fenced.group(0) if fenced else text)


def _clamp(x) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def validate(verdict: dict, allowed_cases: set[int]) -> dict:
    """The model's verdict, checked: known category, confidence in [0, 1],
    TestRail matches only among the cases it was shown."""
    category = verdict.get("category")
    flags = []
    if category not in VERDICT_OF:
        flags.append(f"Unknown category from the AI: {category!r}")
        category = "Needs more information"
    detect = verdict.get("uat_detectability")
    if detect not in VERDICTS:
        flags.append(f"Unknown verdict from the AI: {detect!r}")
        detect = NEEDS_REVIEW
    if VERDICT_OF[category] != detect and VERDICT_OF[category] != NEEDS_REVIEW:
        flags.append(f"The category belongs to {VERDICT_OF[category]}, the verdict says {detect}")
    matches, seen = [], set()
    for m in verdict.get("testrail_matches") or []:
        cid = m.get("case_id")
        if cid in allowed_cases and cid not in seen:
            seen.add(cid)
            matches.append({"case_id": int(cid), "confidence": _clamp(m.get("confidence")),
                            "reason": str(m.get("reason", ""))})
    matches.sort(key=lambda m: -m["confidence"])
    confidence = _clamp(verdict.get("confidence"))
    best = next((m for m in matches if m["confidence"] >= LIKELY), None)
    return {
        "uat_detectability": detect,
        "category": category,
        "functional_area": str(verdict.get("functional_area", "")),
        "confidence": confidence,
        "rationale": str(verdict.get("rationale", "")),
        "evidence": [{"field": str(e.get("field", "")), "value": str(e.get("value", "")),
                      "reason": str(e.get("reason", ""))}
                     for e in (verdict.get("evidence") or [])[:3]],
        "missing_information": [str(x) for x in verdict.get("missing_information") or []],
        "corrective_actions": [str(x) for x in (verdict.get("corrective_actions") or [])[:3]],
        "testrail_matches": matches[:3],
        # The proposal for the Key QA's "TestRail case" column: only a match
        # the AI is reasonably sure of — a guess is not coverage.
        "testrail_case": f"C{best['case_id']}" if best else "",
        "flags": flags,
        "requires_review": bool(flags) or detect == NEEDS_REVIEW or confidence < LIKELY,
    }


def classify(rows: list[dict], context: dict[str, tuple[list, list]]
             ) -> tuple[dict[str, dict], str | None, list[str]]:
    """({key: validated verdict}, model that answered, errors).

    `context[key]` = (linked [(CaseInfo, why)], candidates [CaseInfo]).
    Batches of BATCH incidents; a failed batch is reported and skipped, the
    others still count."""
    if not gemini_client.ready():
        return {}, None, ["Gemini is not configured (GEMINI_API_KEY)."]
    types = gemini_client.types
    config = types.GenerateContentConfig(
        system_instruction=system_instruction(),
        response_mime_type="application/json",
        response_schema=response_schema(),
        temperature=0.1,
    )
    out: dict[str, dict] = {}
    errors: list[str] = []
    model_used: str | None = None
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        prompt = "\n\n".join(incident_block(r, *context[r["key"]]) for r in batch)
        result = gemini_client.generate(
            [types.Content(role="user", parts=[types.Part(text=prompt)])],
            config, gemini_client.ANALYSIS_CHAIN, gemini_client.shared_cooldowns())
        if result.model is None:
            errors.append(gemini_client.failure_message(result.error))
            break                              # every model refused: stop asking
        model_used = model_used or result.model
        try:
            verdicts = _json_payload(result.text).get("verdicts") or []
        except (ValueError, AttributeError):
            errors.append(f"{result.model} answered with unreadable JSON for "
                          f"{', '.join(r['key'] for r in batch)}.")
            continue
        wanted = {r["key"] for r in batch}
        for v in verdicts:
            key = str(v.get("key", "")).strip().upper()
            if key in wanted:
                linked, cands = context[key]
                allowed = {c.case_id for c, _w in linked} | {c.case_id for c in cands}
                out[key] = validate(v, allowed)
        missing = wanted - out.keys()
        if missing:
            errors.append(f"No verdict came back for {', '.join(sorted(missing))}.")
    return out, model_used, errors
