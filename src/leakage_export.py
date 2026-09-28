"""The Leakage analysis of one release as an Excel workbook.

Four sheets, all for the release and BU on screen:
  Summary      the release, its window and every figure of the tab
  Incidents    each counted incident: Jira data and the AI proposal (verdict,
               category, confidence, rationale, evidence, TestRail matches,
               actions), in separate, labelled columns
  UAT issues   the Bugs and Defects the ratio divides by, and those left out
  Excluded     the incidents Delivery's rules leave out, with the reason
"""
from __future__ import annotations

import io

import pandas as pd

from .leakage import LEAKAGE_HH_THRESHOLD, LEAKAGE_THRESHOLD, ReleaseLeakage
from .leakage_store import Record


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


def summary_rows(data: ReleaseLeakage, extra: dict[str, object]) -> list[dict]:
    w = data.window
    rows = [
        ("Business Unit", data.group.label),
        ("Jira project", data.group.project),
        ("Release (fixVersion)", data.release.name),
        ("Release date", data.release.date.isoformat() if data.release.date else ""),
        ("Released", "yes" if data.release.released else "no"),
        ("Previous release", f"{data.previous.name} ({data.previous.date})" if data.previous else ""),
        ("Post-release window",
         (f"{w.start} → {w.end}" + (" (to date)" if w.open_ended else "")) if w else "not released"),
        ("Next release", f"{w.following.name} ({w.following.date})" if w and w.following else ""),
        ("UAT issues (Bugs + Defects since the previous release, in the fixVersion)",
         len(data.uat)),
        ("  of which Bugs", data.uat_bugs),
        ("  of which Defects", data.uat_defects),
        ("Older Defects of the fixVersion, not counted", len(data.uat_left_out)),
        ("Leaked production incidents", len(data.leaks)),
        ("  of which High / Highest", data.leaks_hh),
        ("Incidents excluded by Delivery's rules", len(data.excluded)),
        ("Leakage ratio", _pct(data.ratio)),
        ("Leakage ratio threshold", _pct(LEAKAGE_THRESHOLD)),
        ("Leakage ratio High/Highest", _pct(data.ratio_hh)),
        ("Leakage ratio High/Highest threshold", _pct(LEAKAGE_HH_THRESHOLD)),
    ]
    rows += list(extra.items())
    return [{"Field": k, "Value": v} for k, v in rows]


def incident_rows(data: ReleaseLeakage, records: dict[str, Record],
                  gaps: dict[str, str]) -> list[dict]:
    out = []
    for r in data.leaks:
        rec = records.get(r["key"], Record())
        ai = rec.ai or {}
        out.append({
            "Incident": r["key"], "Link": r["url"], "Summary": r["summary"],
            "Created": r["created"].strftime("%Y-%m-%d") if r["created"] else "",
            "Priority": r["priority"], "Status": r["status"],
            "Components": ", ".join(r["components"]), "Labels": ", ".join(r["labels"]),
            "Environment": r["environment"], "Root cause (Jira)": r["root_cause"],
            "AI verdict": ai.get("uat_detectability", ""),
            "AI category": ai.get("category", ""),
            "AI confidence": ai.get("confidence", ""),
            "AI functional area": ai.get("functional_area", ""),
            "AI rationale": ai.get("rationale", ""),
            "AI evidence": " | ".join(f"{e['field']}: {e['value']} ({e['reason']})"
                                      for e in ai.get("evidence", [])),
            "AI missing information": ", ".join(ai.get("missing_information", [])),
            "AI TestRail matches": " | ".join(
                f"C{m['case_id']} ({m['confidence']:.2f}): {m['reason']}"
                for m in ai.get("testrail_matches", [])),
            "AI corrective actions": " | ".join(ai.get("corrective_actions", [])),
            "AI TestRail case": ai.get("testrail_case", ""),
            "Coverage gap": gaps.get(r["key"], ""),
            "AI model": rec.ai_model, "AI analysed at": rec.ai_at,
        })
    return out


def workbook(data: ReleaseLeakage, records: dict[str, Record], gaps: dict[str, str],
             extra_summary: dict[str, object]) -> bytes:
    sheets = {
        "Summary": pd.DataFrame(summary_rows(data, extra_summary)),
        "Incidents": pd.DataFrame(incident_rows(data, records, gaps)),
        "UAT issues": pd.DataFrame([{
            "Issue": r["key"], "Link": r["url"], "Type": r["type"],
            "Created": r["created"].strftime("%Y-%m-%d") if r["created"] else "",
            "Priority": r["priority"], "Status": r["status"], "Summary": r["summary"],
            "Counted": "no" if r.get("excluded_because") else "yes",
            "Why not": r.get("excluded_because", ""),
        } for r in [*data.uat, *data.uat_left_out]]),
        "Excluded": pd.DataFrame([{
            "Incident": r["key"], "Link": r["url"], "Excluded because": r["excluded_because"],
            "Created": r["created"].strftime("%Y-%m-%d") if r["created"] else "",
            "Priority": r["priority"], "Root cause (Jira)": r["root_cause"],
            "Summary": r["summary"],
        } for r in data.excluded]),
    }
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for name, frame in sheets.items():
            frame.to_excel(xl, sheet_name=name, index=False)
    return buf.getvalue()
