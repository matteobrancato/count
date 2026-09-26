"""When the dashboard's numbers go stale: once a day, at the first visit.

Asked for on 2026-09-26: load once, then keep it for the whole day, so every
visit after the first is instant.  So there is no timed refresh any more.  The
first run of each business day finds numbers stamped on an earlier day, clears
every cache and reloads from TestRail; every other run that day is a cache hit.
The ↻ in the freshness bar still forces a reload at any time.

Everything here lives in an IMPORTED module on purpose.  Streamlit re-executes
the main script from scratch on every rerun, so module-level state in app.py —
a lock, a flag — is recreated each time and shared by no one.  The old
refresh watchdog kept its "single-flight across sessions" lock there, and it
never protected anything.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import streamlit as st

logger = logging.getLogger(__name__)

# The business day the team works in.  Streamlit Cloud runs on UTC, so without
# this the day would turn over at 01:00 or 02:00 local time.
try:
    TZ = ZoneInfo("Europe/Rome")
except ZoneInfoNotFoundError:
    logger.warning("Europe/Rome is unknown on this host; the daily reload uses UTC")
    TZ = timezone.utc

# TTL for every cache that is NOT persisted to disk.  Not what refreshes the
# numbers — the daily rollover clears everything explicitly — only a backstop
# bounding how stale a derived frame could get if that ever failed.  A day
# long, so no cache can expire in the middle of one.
DAY_TTL = 24 * 3600

_ROLLOVER_LOCK = threading.Lock()


@st.cache_data(show_spinner=False, persist="disk")
def stamp() -> float:
    """Wall-clock time the current cached numbers were fetched.

    Persisted with the TestRail payloads it describes, so a restart cannot
    re-label day-old numbers "Updated just now".  No `ttl`: Streamlit ignores
    it on persisted caches (and logs a warning saying so on every boot).
    """
    return time.time()


def business_day(ts: float) -> date:
    return datetime.fromtimestamp(ts, TZ).date()


def clear_everything() -> None:
    """Forget every cached number, so the next run reloads from TestRail.

    `st.cache_data.clear()` rather than a list of caches.  The ↻ button and
    the old watchdog each kept a hand-written list, the two had drifted apart,
    and neither cleared the Production Sanity run or the tile downloads — after
    a refresh the tiles showed new counts while their CSV held the old rows.
    """
    from . import testrail_client as tr  # lazy: testrail_client imports DAY_TTL

    st.cache_data.clear()
    tr.reset_warm_state()


def roll_over_if_new_day() -> bool:
    """Clear everything if the numbers were fetched on an earlier business day.

    Returns True when this call did the clearing.  Checked again under the
    lock: when several people open the dashboard in the morning, the first one
    clears and restamps, and the rest see today's stamp and leave the reload
    it started alone — a second clear would throw away a download in progress
    at a rate limit that makes every request count.
    """
    today = business_day(time.time())
    if business_day(stamp()) == today:
        return False
    with _ROLLOVER_LOCK:
        if business_day(stamp()) == today:
            return False
        logger.warning("New business day %s: clearing yesterday's numbers", today)
        clear_everything()
        stamp()          # restamp now, so every other session sees today
        return True
