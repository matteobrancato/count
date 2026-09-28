"""Leakage tab — production incidents from Jira, per Business Unit.

Follows the global BU selector; the scope selector does not apply (Jira tracks
incidents per project, not per website / app scope — the Web / App split comes
from the incidents' own components).  All the logic lives in `src/leakage.py`.

Cost: one Jira read of the last year's incidents, cached for the day and shared
by every session; the match with TestRail uses cases already downloaded for
the Backlog tab, so this tab never calls TestRail.
"""
from __future__ import annotations

import html
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

import altair as alt
import pandas as pd
import streamlit as st

from .. import jira_client
from .. import leakage as lk
from . import global_filter
from .styles import COLORS, section_title, stat_card

logger = logging.getLogger(__name__)

_PRIORITY_ORDER = ("Highest", "High", "Medium", "Low", "Lowest", "Not set")


def _links() -> tuple[dict[str, set[int]], set[int]]:
    """({Jira key: citing case ids}, automated case ids) from cached frames.

    Cache hits: the Backlog tab has loaded both scopes by the time this runs.
    """
    from .backlog_tab import _load_scope

    refs: dict[int, str] = {}
    automated: set[int] = set()
    for scope in ("website", "next_gen"):          # never trigger the deferred MAPP load
        try:
            raw, auto, _rules = _load_scope(scope)
        except Exception:
            logger.exception("Leakage: could not read the %s cases", scope)
            continue
        if not raw.empty and "refs" in raw.columns:
            for case_id, r in zip(raw["case_id"], raw["refs"]):
                if r:
                    refs[int(case_id)] = r
        if not auto.empty:
            automated |= set(auto["case_id"].astype(int))
    return lk.cases_by_key(refs), automated


def _change_badge(current: int, previous: int, period: str) -> str:
    pct = lk.change(current, previous)
    if pct is None:
        return ""
    up = pct > 0
    colour = COLORS["danger"] if up else COLORS["success"]   # more incidents is worse
    arrow = "▲" if up else ("▼" if pct < 0 else "■")
    # Only the percentage: the card is narrow, and the comparison it refers
    # to is spelled out in the caption under it.
    return (f"<span style='font-size:13px;font-weight:650;color:{colour};"
            f"white-space:nowrap'>{arrow} {pct:+.0f}%</span>")


def _trend(rows: list[dict], now: datetime) -> alt.Chart:
    """Incidents per calendar month, last six months — one series, one hue."""
    months: list[str] = []
    cursor = now.replace(day=1)
    for _ in range(6):
        months.append(cursor.strftime("%Y-%m"))
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months.reverse()
    by_month: dict[str, Counter] = {m: Counter() for m in months}
    for r in rows:
        if r["month"] in by_month:
            by_month[r["month"]][r["priority"]] += 1
    current = now.strftime("%Y-%m")
    data = pd.DataFrame([{
        "month": datetime.strptime(m, "%Y-%m").replace(tzinfo=timezone.utc).strftime("%b %Y")
                 + (" (to date)" if m == current else ""),
        "incidents": sum(c.values()),
        "by_priority": " · ".join(f"{p} {c[p]}" for p in _PRIORITY_ORDER if c[p]) or "none",
    } for m, c in by_month.items()])
    return (
        alt.Chart(data)
        .mark_bar(size=34, cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                  color=COLORS["brand"])
        .encode(
            x=alt.X("month:N", sort=None, title=None,
                    axis=alt.Axis(labelAngle=0, domain=False, ticks=False,
                                  labelColor=COLORS["muted"], labelFontSize=12)),
            y=alt.Y("incidents:Q", title=None,
                    axis=alt.Axis(domain=False, ticks=False, tickMinStep=1,
                                  gridColor=COLORS["grid"],
                                  labelColor=COLORS["muted"])),
            tooltip=[alt.Tooltip("month:N", title="Month"),
                     alt.Tooltip("incidents:Q", title="Incidents"),
                     alt.Tooltip("by_priority:N", title="By priority")],
        )
        .properties(height=220)
        .configure_view(stroke=None)
    )


def _bu_section(rows: list[dict], group: lk.Group, days: int, period: str,
                now: datetime, by_key: dict[str, set[int]],
                automated: set[int]) -> None:
    mine = [r for r in rows if r["group"] == group.label]
    current = lk.window(mine, days, now)
    previous = lk.window(mine, days, now, offset=days)

    section_title(f"Production incidents · {html.escape(group.label)}")
    if group.shared:
        st.caption(f"One Jira project ({', '.join(group.projects)}) serves "
                   f"{', '.join(group.bus)}: its incidents cannot be split per "
                   f"Business Unit, so they are shown together.")

    high = sum(1 for r in current if r["priority"] in ("Highest", "High"))
    channels = Counter(r["channel"] for r in current)
    cover = Counter(lk.coverage(r["key"], by_key, automated) for r in current)

    c1, c2, c3, c4 = st.columns(4)
    stat_card(c1, f"Incidents · last {period}", len(current),
              badge_html=_change_badge(len(current), len(previous), period))
    c1.caption(f"{len(previous):,} in the previous {period}")
    stat_card(c2, "Highest & High", high)
    stat_card(c3, "App", channels["App"])
    c3.caption(f"{channels['Web']:,} web · {channels[lk.NO_COMPONENT]:,} "
               f"without a component")
    stat_card(c4, "On an automated test", cover[lk.COVERED_AUTOMATED])
    c4.caption(f"{cover[lk.COVERED_MANUAL]:,} on tests not automated · "
               f"{cover[lk.NOT_LINKED]:,} with no linked test")

    st.markdown("<div style='height:6px'></div>", unsafe_allow_html=True)
    st.altair_chart(_trend(mine, now), width="stretch")

    if current:
        table = pd.DataFrame([{
            "Incident": r["url"],
            "Created": r["created"].date(),
            "Priority": r["priority"],
            "Status": r["status"],
            "Environment": r["environment"] or "—",
            "Channel": r["channel"],
            "Test coverage": lk.coverage(r["key"], by_key, automated),
            "Summary": r["summary"],
        } for r in sorted(current, key=lambda r: r["created"], reverse=True)])
        with st.expander(f"The {len(current):,} incidents of the last {period}"):
            st.dataframe(
                table, hide_index=True, width="stretch", height=360,
                column_config={"Incident": st.column_config.LinkColumn(
                    display_text=r".*/browse/(.*)")})


def _all_groups(rows: list[dict], days: int, period: str, now: datetime,
                by_key: dict[str, set[int]], automated: set[int]) -> None:
    section_title(f"All Business Units · last {period}", top=10)
    labels = [g.label for g in lk.GROUPS]
    if any(r["group"] == lk.OTHER for r in rows):
        labels.append(lk.OTHER)
    out: list[dict] = []
    for label in labels:
        mine = [r for r in rows if r["group"] == label]
        cur = lk.window(mine, days, now)
        prev = lk.window(mine, days, now, offset=days)
        pct = lk.change(len(cur), len(prev))
        out.append({
            "Business Unit": label,
            "Incidents": len(cur),
            f"Previous {period}": len(prev),
            # Numeric, so sorting the column orders +12% after +6%.
            "Change": pct,
            "Highest & High": sum(1 for r in cur if r["priority"] in ("Highest", "High")),
            "App": sum(1 for r in cur if r["channel"] == "App"),
            "On an automated test": sum(
                1 for r in cur
                if lk.coverage(r["key"], by_key, automated) == lk.COVERED_AUTOMATED),
        })
    table = pd.DataFrame(out)
    # Busiest first; the unmapped projects always last, whatever their count.
    table = pd.concat([
        table[table["Business Unit"] != lk.OTHER].sort_values("Incidents", ascending=False),
        table[table["Business Unit"] == lk.OTHER],
    ])
    st.dataframe(table, hide_index=True, width="stretch", column_config={
        "Change": st.column_config.NumberColumn("Change", format="%+.0f%%")})


@st.fragment
def render() -> None:
    if not jira_client.available():
        st.info("Leakage reads production incidents from Jira — add JIRA_URL, "
                "ATLASSIAN_USER and ATLASSIAN_API_KEY to the app secrets.")
        return

    top_left, top_right = st.columns([2.2, 1.8], vertical_alignment="center")
    top_left.caption(f"Every Jira “{lk.ISSUE_TYPE}”, in any environment, by the "
                     "Jira project of each Business Unit. Refreshed once a day, with the "
                     "rest of the dashboard.")
    period = top_right.segmented_control(
        "Period", list(lk.PERIODS), default="90 days", key="lk_period",
        label_visibility="collapsed") or "90 days"
    days = lk.PERIODS[period]

    try:
        with st.spinner("Reading production incidents from Jira…"):
            rows, truncated = lk.fetch()
    except Exception as exc:                                            # noqa: BLE001
        st.warning(f"Production incidents could not be read from Jira right now "
                   f"({exc}). Try again in a few minutes.")
        return
    if truncated:
        st.warning(f"Jira returned more than {lk.MAX_PAGES * 100:,} incidents in a "
                   "year — the oldest were not read, so long-period counts may be low.")

    now = datetime.now(timezone.utc)
    by_key, automated = _links()
    _scope, bu = global_filter.current()
    group = lk.group_for_bu(bu) if bu else None
    if group is None:
        st.info(f"No Jira project is mapped to **{bu}**: incidents are tracked per "
                "Jira project, and this Business Unit has none of its own. "
                "The table below covers every project.")
    else:
        _bu_section(rows, group, days, period, now, by_key, automated)

    st.divider()
    _all_groups(rows, days, period, now, by_key, automated)
