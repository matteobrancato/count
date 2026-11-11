"""Thin TestRail API wrapper with pagination, retries and Streamlit caching.

TestRail v2 APIs (get_cases, get_sections, ...) return paginated envelopes:
    {"offset": 0, "limit": 250, "size": N, "_links": {"next": "...", "prev": "..."}, "cases": [...]}
When "next" is null we are done. We follow the next link (relative) until exhausted.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .freshness import DAY_TTL

logger = logging.getLogger(__name__)


class TestRailError(RuntimeError):
    pass


# Every cached read below is persisted to disk.  A cold download is ~200
# paginated requests, so at the cap TestRail currently enforces it is measured
# in minutes, not seconds — and until this was persisted a script restart threw
# it away and paid it again, which is what "the dashboard stopped loading"
# actually was.  The payloads are lists of dicts, so they pickle cleanly.
#
# These caches take no `ttl`: Streamlit ignores it on persisted caches (and
# logged a warning per cache on every boot saying so).  They are cleared
# explicitly instead — once per business day, or by ↻ — see `src/freshness.py`,
# which persists the "fetched at" stamp for the same reason, so the "Updated"
# label ages with the data rather than with the process.

# ── global request pacer ──────────────────────────────────────────────────────
# The limiter is the whole performance story, and its number MOVES.  The 429
# body has said "180 per minute" (when this pacer was written), "50 per minute"
# (2026-08-14) and "5 per minute" (2026-08-17) — and every one of those changes
# broke the dashboard in exactly the same way: we kept firing at the old rate,
# every request came back 429, each retry slept into a window the retries
# themselves were holding shut, and the load never finished.  Re-editing a
# constant after each outage is not a fix; the third repeat is what
# "lentissimo e non carica più" was.
#
# So the rate is no longer a constant we maintain: it is LEARNED, in both
# directions, from the one authority on it — TestRail's 429 body, which names
# the cap it enforces.  TestRail sends no rate-limit header on a successful
# response (checked 2026-09-26), so a refusal is the only place the number
# appears, and a client that never goes past its current guess never finds out
# the cap went up.  That is exactly what happened when TestRail began raising
# it again: the cap was 20 while the dashboard still paced for 5.
#
# Hence one probe per download (`_start_probe_window`): pace at TWICE the last
# cap we know, never above the ceiling (TESTRAIL_RATE_LIMIT, else TestRail's
# historical 180).  If TestRail refuses, its 429 states the exact cap and the
# rest of the download paces at it; if it does not, the higher rate stands and
# the next download tries twice that.  Doubling tracks a cap raised step by
# step in a handful of downloads.
#
# Why overshooting is no longer the disaster it was in August:
#   * the cap is per MINUTE, so spending the minute's budget in its first
#     seconds and then waiting delivers the same throughput as spreading it;
#   * the first 429 re-paces AND backs every queued slot off together
#     (`_pace_cooldown`), so only the requests already in flight are refused —
#     a handful, not the eighty-request avalanche of a client with no pacer;
#   * a 429 whose body cannot be read HALVES the pace, so even a TestRail
#     wording change converges instead of hammering.
# Within one download the cap is still only ever lowered by a 429.
#
# Why a pacer at all: the warm-up fires up to ~80 concurrent requests.  Against
# a limiter that is an avalanche — one 429 becomes eighty, a browser refresh
# starts a second wave over the first, and nothing reaches the cache so the
# next session pays it all again.  Every request from every thread and session
# (same process) instead reserves a time slot, so the cold download is
# deterministic and produces essentially zero 429s.
#
# ONE account.  Up to 2026-11 the client pooled several (TESTRAIL_USER_1.._8)
# on the belief that the cap is per user, so N accounts would give N times the
# budget.  Measured, it is not: four accounts on 2026-09-26 and three on
# 2026-11-11 both moved ~20 requests/minute IN TOTAL — one account's cap — and
# the pool, pacing for three times that, spent the download in 429 pauses.  The
# limit is the instance's (or the IP's), so a second account buys nothing.
_PACE_LOCK = threading.Lock()
_PACE_NEXT = 0.0
_PACE_EPOCH = 0     # bumped by every cooldown; see `_pace`

# The CEILING: the pacer never asks for more than this, whatever it learns.
# 180 is the level TestRail enforced before August 2026, and the one Support
# said they would restore and lock.  TESTRAIL_RATE_LIMIT in the secrets
# overrides it — as a ceiling now, no longer as the rate itself.
_CEILING_DEFAULT = 180
# Where learning starts in a fresh process, before any 429 has spoken: the cap
# TestRail stated on 2026-09-26.  It only seeds the first probe (which asks for
# twice this), so a stale value costs one extra doubling, never a wrong rate.
_SEED_LIMIT = 20
# 15% headroom.  Pacing at exactly the cap sits on the boundary, where our
# clock and TestRail's disagree by more than enough to 429 anyway.
_HEADROOM = 0.85


def _paced_per_account(limit: int) -> float:
    """Requests/minute to actually issue on ONE account.

    A PERCENTAGE of headroom is the wrong shape once the cap is small.  15% of
    180 is twenty-seven requests of slack; 15% of 5 is three quarters of ONE.
    At a cap of 5 the pacer therefore ran each account at 4.25 req/min with
    less than a single request left to absorb every source of jitter — our
    clock against TestRail's, the window boundary, the unpaced pool probe — and
    the 2026-09-11 cold start 429'd on essentially every request for seventeen
    minutes without ever finishing.

    So the slack is now whichever is SMALLER: the old percentage, or one whole
    request.  At 5/min that paces 4 and keeps a full request in reserve; at
    180/min the percentage still dominates and the behaviour is unchanged.
    The floor at half the cap stops a cap of 1 from pacing to zero.
    """
    return max(limit * 0.5, min(limit * _HEADROOM, limit - 1.0))


_LIMIT_LOCK = threading.Lock()
_limit_declared = _CEILING_DEFAULT    # the ceiling: secrets, or the default
_limit_observed: int | None = None    # what a 429 told us in this window
_probe_target = _SEED_LIMIT           # what we pace at until a 429 speaks
_PACE_INTERVAL = 60.0 / _paced_per_account(_SEED_LIMIT)

# "API Rate Limit Exceeded - 5 per minute maximum allowed. Retry after 57s."
_LIMIT_RE = re.compile(r"(\d+)\s+per\s+minute\s+maximum\s+allowed", re.I)


def _effective_limit() -> int:
    """Requests/minute to pace ONE account at, never above the ceiling.

    What TestRail stated in this window when it has spoken, otherwise the
    current probe target.
    """
    current = _limit_observed if _limit_observed is not None else _probe_target
    return max(1, min(_limit_declared, current))


def _start_probe_window() -> None:
    """Try twice the last known cap for the download about to start.

    Called once per download (see `prefetch_all_suites`): the one moment a lot
    of requests go out, so a raised cap is found as soon as it exists, and a
    cap that has not moved costs only the few requests in flight when TestRail
    says so.  Between downloads the pace stays where the last one left it.
    """
    global _limit_observed, _probe_target
    with _LIMIT_LOCK:
        base = _limit_observed if _limit_observed is not None else _probe_target
        _probe_target = min(_limit_declared, max(base * 2, base + 5))
        _limit_observed = None
        _repace()
    logger.warning(
        "TestRail pacing: probing %d requests/minute this download "
        "(last known %d, ceiling %d)", _probe_target, base, _limit_declared)


def _repace() -> None:
    """Recompute the slot interval from the limit."""
    global _PACE_INTERVAL
    _PACE_INTERVAL = 60.0 / max(0.1, _paced_per_account(_effective_limit()))


def _learn_limit(body: str) -> None:
    """Take TestRail's word for the cap — within a window, downwards only.

    A 429 whose body does not name a number still means "too fast": the pace
    is halved, so a change of wording on TestRail's side converges instead of
    leaving the pacer asking for the same refused rate over and over.
    """
    global _limit_observed
    match = _LIMIT_RE.search(body or "")
    with _LIMIT_LOCK:
        if match:
            told = max(1, int(match.group(1)))
            if _limit_observed is not None and told >= _limit_observed:
                return
        else:
            told = max(1, _effective_limit() // 2)
        _limit_observed = told
        _repace()
    logger.warning(
        "TestRail says the cap is %d requests/minute%s — re-pacing to %.0f/min "
        "(slot every %.1fs)",
        told, "" if match else " (unreadable 429: halved)",
        60 / _PACE_INTERVAL, _PACE_INTERVAL)


def _configured_limit() -> int:
    """The ceiling: TESTRAIL_RATE_LIMIT from the secrets, else TestRail's 180."""
    try:
        return max(1, int(st.secrets["TESTRAIL_RATE_LIMIT"]))
    except Exception:                                                   # noqa: BLE001
        return _CEILING_DEFAULT


_STATS_LOCK = threading.Lock()
_REQUESTS_SERVED = 0


def requests_served() -> int:
    """Successful API calls this process has made.

    The loader's heartbeat.  Suite-level progress ticks once every ~80s at the
    current cap, which on a twelve-minute download is indistinguishable from a
    hang; this moves with every single request.
    """
    return _REQUESTS_SERVED


# ── single-flight ─────────────────────────────────────────────────────────────
# st.cache_data does NOT deduplicate concurrent MISSES: when several sessions
# hit the same cold key together (e.g. the user refreshes during the first
# load), each one recomputes — we measured FOUR parallel full downloads
# queueing on the pacer (338s instead of ~70s).  These per-key locks make
# every concurrent caller WAIT for the first computation and then hit the
# fresh cache entry instead of re-downloading.
_SF_GUARD = threading.Lock()
_SF_LOCKS: dict[tuple, threading.Lock] = {}


def _sf_lock(key: tuple) -> threading.Lock:
    with _SF_GUARD:
        return _SF_LOCKS.setdefault(key, threading.Lock())


def _pace() -> None:
    """Block until this thread's reserved request slot arrives.

    A slot reserved BEFORE a cooldown is not honoured after it: the cooldown
    means TestRail has just closed the window that slot fell in.  Pushing
    `_PACE_NEXT` only moved the slots handed out afterwards, so every thread
    already asleep on an earlier one still fired into the closed window — in a
    simulated cold download at a 20/min cap that was 16 refusals out of the 16
    worker threads.  Such a thread now takes a fresh slot instead.
    """
    global _PACE_NEXT
    while True:
        with _PACE_LOCK:
            now = time.time()
            wait = _PACE_NEXT - now
            _PACE_NEXT = max(now, _PACE_NEXT) + _PACE_INTERVAL
            epoch = _PACE_EPOCH
        if wait > 0:
            time.sleep(wait)
        with _PACE_LOCK:
            if _PACE_EPOCH == epoch:
                return


def _pace_cooldown(seconds: float) -> None:
    """Push EVERY queued slot past the window TestRail just closed.

    The thread that collected the 429 always waited politely.  The other
    seventy-nine kept firing into the same closed window — so one 429 became
    eighty, and the retries were what held the window shut.  Backing the whole
    pacer off together is what turns an avalanche into a pause.  The epoch
    tells the threads already asleep on a slot that it no longer counts.
    """
    global _PACE_NEXT, _PACE_EPOCH, _COOLDOWN_UNTIL
    with _PACE_LOCK:
        _PACE_NEXT = max(_PACE_NEXT, time.time() + seconds)
        _PACE_EPOCH += 1
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.time() + seconds)


# When the pool may send again after TestRail refused a request.  Read by the
# loader: a request counter that stops for a minute reads as a hang, the same
# minute labelled "TestRail asked us to wait" reads as what it is.
_COOLDOWN_UNTIL = 0.0


def cooldown_remaining() -> float:
    """Seconds until TestRail's rate limit lets us send again, else 0."""
    return max(0.0, _COOLDOWN_UNTIL - time.time())


@dataclass(frozen=True)
class TestRailCredentials:
    base_url: str
    user: str
    api_key: str

    @classmethod
    def from_secrets(cls) -> "TestRailCredentials":
        """TESTRAIL_URL, TESTRAIL_USER and TESTRAIL_API_KEY from the secrets."""
        try:
            url = st.secrets["TESTRAIL_URL"].rstrip("/")
            user = st.secrets["TESTRAIL_USER"]
            key = st.secrets["TESTRAIL_API_KEY"]
        except Exception as exc:
            raise TestRailError(
                "Missing TestRail secrets. Add TESTRAIL_URL, TESTRAIL_USER, "
                "TESTRAIL_API_KEY to .streamlit/secrets.toml or the Streamlit Cloud secrets panel."
            ) from exc
        if not str(user).strip() or not str(key).strip():
            raise TestRailError("Empty TestRail credentials: TESTRAIL_USER / TESTRAIL_API_KEY")
        return cls(base_url=url, user=user, api_key=key)


class TestRailClient:
    """Lightweight TestRail client. Instances are cheap — reuse the underlying Session."""

    def __init__(self, creds: TestRailCredentials, timeout: int = 60) -> None:
        self.creds = creds
        self.timeout = timeout
        self._session = self._make_session(creds)

    def _make_session(self, creds: TestRailCredentials) -> requests.Session:
        session = requests.Session()
        session.auth = HTTPBasicAuth(creds.user, creds.api_key)
        session.headers.update({"Content-Type": "application/json"})
        # Big connection pool — the cold-start warm-up fires many parallel
        # requests (16 suite workers × up to 5 pagination workers each ≈ 80
        # peak).  The default urllib3 pool is only 10 connections, so without
        # this the parallel fetches silently queue behind 10 sockets.  maxsize
        # is a cap, not a preallocation, so oversizing is free — 96 covers the
        # warm-up peak without connection churn (discard/reopen).
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=96, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    # ------------------------------------------------------------------ low level
    def _url(self, endpoint: str) -> str:
        # Accepts any of: "get_cases/1&suite_id=2", "api/v2/get_cases/...",
        # "/api/v2/get_cases/...", or a full "index.php?/api/v2/..." path.
        endpoint = endpoint.lstrip("/")
        if endpoint.startswith("index.php"):
            return urljoin(self.creds.base_url + "/", endpoint)
        if endpoint.startswith("api/v2/"):
            endpoint = endpoint[len("api/v2/"):]
        return urljoin(self.creds.base_url + "/", f"index.php?/api/v2/{endpoint}")

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    )
    def _get(self, endpoint: str) -> Any:
        _pace()
        resp = self._session.get(self._url(endpoint), timeout=self.timeout)
        # Rate limit — honour Retry-After (seconds form; RFC 7231 also allows an
        # HTTP-date, which int() can't parse).  THREE attempts, not one: TestRail
        # asks for 41-57s when the window is full and the old 30s cap gave up
        # before it reopened, turning a wait into a failed load.  Capped so a
        # hostile or buggy header cannot stall a worker thread indefinitely.
        for _ in range(3):
            if resp.status_code != 429:
                break
            # The body names the real cap.  Reading it here is what stops the
            # next outage: whatever TestRail moves the number to, the pacer
            # follows within one request instead of within one commit.
            _learn_limit(resp.text)
            try:
                wait = min(int(resp.headers.get("Retry-After", "10")), 60)
            except ValueError:
                wait = 10
            _pace_cooldown(wait)
            time.sleep(wait)
            _pace()
            resp = self._session.get(self._url(endpoint), timeout=self.timeout)
        if not resp.ok:
            raise TestRailError(f"GET {endpoint} → {resp.status_code}: {resp.text[:300]}")
        global _REQUESTS_SERVED
        with _STATS_LOCK:
            _REQUESTS_SERVED += 1
        try:
            return resp.json()
        except ValueError as exc:
            raise TestRailError(f"Invalid JSON from {endpoint}: {exc}") from exc

    # --------------------------------------------------------------------- public
    def get_case_fields(self) -> list[dict]:
        return self._get("get_case_fields")

    def get_case_types(self) -> list[dict]:
        return self._get("get_case_types")

    def get_priorities(self) -> list[dict]:
        return self._get("get_priorities")

    def get_suite(self, suite_id: int) -> dict:
        return self._get(f"get_suite/{suite_id}")

    def get_labels(self, project_id: int, limit: int = 250) -> list[dict]:
        """Fetch all labels defined for a project (native TR labels, not custom fields)."""
        labels: list[dict] = []
        offset = 0
        while True:
            data = self._get(f"get_labels/{project_id}&offset={offset}&limit={limit}")
            chunk = data.get("labels", []) if isinstance(data, dict) else data
            labels.extend(chunk)
            if len(chunk) < limit:
                break
            offset += limit
        return labels

    def get_sections(self, project_id: int, suite_id: int) -> list[dict]:
        out: list[dict] = []
        endpoint = f"get_sections/{project_id}&suite_id={suite_id}"
        while endpoint:
            payload = self._get(endpoint)
            if isinstance(payload, list):  # old TR without pagination envelope
                return payload
            out.extend(payload.get("sections", []))
            nxt = (payload.get("_links") or {}).get("next")
            endpoint = nxt.lstrip("/") if nxt else None
        return out

    def get_cases(self, project_id: int, suite_id: int, limit: int = 250) -> list[dict]:
        """Every case in a suite, following TestRail's own `_links.next`.

        Sequential ON PURPOSE, like `get_sections`.  This
        used to speculate five pages ahead, which was free while requests were
        cheap — the pacer serialises every request anyway, so the parallelism
        never bought wall-clock here.  What it did buy was the pages *past* the
        end of each suite: a 4-page suite cost 6 requests, and the whole batch
        was always awaited before the short page was noticed.  At the cap
        TestRail now enforces those overshoots run to about a minute per cold
        start across the core suites, so we ask for exactly the pages that
        exist.
        """
        endpoint = f"get_cases/{project_id}&suite_id={suite_id}&limit={limit}&offset=0"
        cases: list[dict] = []
        while endpoint:
            payload = self._get(endpoint)
            if isinstance(payload, list):     # old TR without pagination envelope
                return payload
            page = payload.get("cases", [])
            cases.extend(page)
            nxt = (payload.get("_links") or {}).get("next")
            # An empty page cannot be a legitimate middle page: a `next` that
            # keeps pointing at nothing would otherwise loop forever, and at
            # ~3.5s per request that is a warm-up that never returns.
            endpoint = nxt.lstrip("/") if (nxt and page) else None
        return cases


# --------------------------------------------------------------------- caching
# We cache at the *function* level so Streamlit's cache key includes arguments;
# every cached fetch below goes through the one shared client.
_CLIENT: TestRailClient | None = None
_CLIENT_LOCK = threading.Lock()


def _get_client() -> TestRailClient:
    """The shared client, built once per process.

    Double-checked: the warm-up submits up to eight suite workers at once, and
    they all arrive here before the client exists.
    """
    global _CLIENT, _limit_declared
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None:
            _limit_declared = _configured_limit()
            _repace()
            logger.warning(
                "TestRail: pacing at %.0f requests/minute (cap %d, ceiling %d%s)",
                60 / _PACE_INTERVAL, _effective_limit(), _limit_declared,
                " from TESTRAIL_RATE_LIMIT" if _limit_declared != _CEILING_DEFAULT else "")
            _CLIENT = TestRailClient(TestRailCredentials.from_secrets())
    return _CLIENT


@st.cache_data(show_spinner=False, persist="disk")
def fetch_case_fields() -> list[dict]:
    return _get_client().get_case_fields()


@st.cache_data(show_spinner=False, persist="disk")
def fetch_case_types() -> list[dict]:
    return _get_client().get_case_types()


@st.cache_data(show_spinner=False, persist="disk")
def fetch_priorities() -> list[dict]:
    return _get_client().get_priorities()


@st.cache_data(show_spinner=False, persist="disk")
def fetch_suite(suite_id: int) -> dict:
    return _get_client().get_suite(suite_id)


@st.cache_data(show_spinner=False, persist="disk")
def _fetch_sections_cached(project_id: int, suite_id: int) -> list[dict]:
    return _get_client().get_sections(project_id, suite_id)


def fetch_sections(project_id: int, suite_id: int) -> list[dict]:
    with _sf_lock(("sections", project_id, suite_id)):
        return _fetch_sections_cached(project_id, suite_id)


# Heavy free-text fields stripped from the BULK case cache: nothing in the
# dashboard renders them, and they dominate memory (a case's steps often weigh
# more than every other field combined — dropping them cuts the cached payload
# several-fold, which matters on Streamlit Cloud's ~1GB container: memory
# pressure there means OOM restarts, i.e. "the spinner never stops").
# The single-case `fetch_case` (deep-dive) is intentionally NOT slimmed.
_HEAVY_CASE_FIELDS = (
    "custom_steps_separated", "custom_steps", "custom_preconds",
    "custom_expected", "custom_mission", "custom_goals",
    "custom_testrail_bdd_scenario", "custom_automation_snippet",
)


def _slim_case(case: dict) -> dict:
    for f in _HEAVY_CASE_FIELDS:
        case.pop(f, None)
    return case


@st.cache_data(show_spinner=False, persist="disk")
def _fetch_cases_cached(project_id: int, suite_id: int) -> list[dict]:
    return [_slim_case(c) for c in _get_client().get_cases(project_id, suite_id)]


def fetch_cases(project_id: int, suite_id: int) -> list[dict]:
    with _sf_lock(("cases", project_id, suite_id)):
        return _fetch_cases_cached(project_id, suite_id)


@st.cache_data(show_spinner=False, persist="disk")
def _fetch_labels_cached(project_id: int) -> dict[int, str]:
    """Return {label_id: label_name} for the given project."""
    raw = _get_client().get_labels(project_id)
    return {int(lbl["id"]): lbl.get("title", lbl.get("name", "")) for lbl in raw}


def fetch_labels(project_id: int) -> dict[int, str]:
    with _sf_lock(("labels", project_id)):
        return _fetch_labels_cached(project_id)


def resolve_project_id(suite_id: int) -> int:
    """Get the project_id that owns a given suite (needed for get_cases)."""
    suite = fetch_suite(suite_id)
    return int(suite["project_id"])


# ----------------------------------------------------------------- startup pre-warm
# Wall-clock of the last pre-warm, and how long it holds.  A day: the numbers
# are reloaded once per business day (`freshness.roll_over_if_new_day`), which
# calls `reset_warm_state()` so the next run pre-warms at once.  Nothing
# re-warms on a timer any more.
_WARMED_AT = 0.0
_WARM_INTERVAL = float(DAY_TTL)

# Whether a download is running right now, and how far it has got — shared, so
# a session that arrives while another one is downloading can SHOW that
# download instead of returning at once and then waiting, silently, on the
# per-suite locks.  That silent wait was the second visitor's whole morning.
_PREFETCH_LOCK = threading.Lock()
_PREFETCH_IDLE = threading.Event()
_PREFETCH_IDLE.set()
_prefetch_progress = [0, 0]          # [done, total] of the running download


# How long a failed warm-up holds the lock before another session may retry.
# NOT the full interval: the caches are empty when it fails, so claiming the
# next six hours turns one bad rate-limit window into an afternoon of lazy,
# serial fetches — which is exactly how a single 429 storm used to become a
# dashboard that stayed broken long after TestRail had recovered.
_WARM_RETRY_AFTER = 120.0
# ...and it DOUBLES while the failures keep coming.  A FIXED 120s was the right
# answer to one bad window and the wrong answer to a bad afternoon: during a
# sustained storm every session threw the same 25 parallel tasks back at a
# window that had not reopened, so the retry was part of what held it shut.
# Capped far below the warm interval, so a recovered TestRail is never locked
# out for more than half an hour.
_WARM_RETRY_MAX = 1800.0
_warm_failures = 0     # consecutive failed prefetches; a clean one resets it

# How often the download reports in.  Suite completions are ~80s apart at the
# current cap; this ticks the counter in between so the box always moves.
_PROGRESS_TICK = 2.0


def reset_warm_state() -> None:
    """Let the next run pre-warm immediately, with a clean failure streak.

    Called after the caches are cleared.  Public so callers stop reaching into
    `_WARMED_AT` from outside, which is how the refresh paths used to do it.
    """
    global _WARMED_AT, _warm_failures
    _WARMED_AT = 0.0
    _warm_failures = 0


def prefetch_all_suites(suite_ids: list[int], on_progress=None) -> None:
    """Pre-warm fetch_cases + fetch_sections + fetch_labels for every suite.

    Fault-tolerant per suite: one deleted/renamed suite must not blank the
    whole dashboard — its failure is logged and skipped here, and if the suite
    genuinely matters, `evaluate_rules` will surface a visible error for it.

    *on_progress* is an optional callback(done, total, requests) fired at least
    every couple of seconds — from THIS thread, never from a worker, so the UI
    call is always made where Streamlit expects it.
    """
    global _WARMED_AT, _warm_failures

    def report(done: int, total: int) -> None:
        if not on_progress:
            return
        try:
            on_progress(done, total, _REQUESTS_SERVED)
        except BaseException:                                           # noqa: BLE001
            # See evaluate_rules' progress hook: a killed session's UI callback
            # must not abort a download the other sessions are waiting on.
            pass

    # Claim the download and mark it running in ONE step, so no session can
    # see "warmed" before the running flag is up.
    with _PREFETCH_LOCK:
        claimed = time.time() - _WARMED_AT >= _WARM_INTERVAL
        if claimed:
            _WARMED_AT = time.time()
            _prefetch_progress[:] = [0, 0]
            _PREFETCH_IDLE.clear()
    if not claimed:
        # Someone else's download — follow it, then return.  Returns at once
        # when nothing is running (the data is simply warm).
        while not _PREFETCH_IDLE.wait(_PROGRESS_TICK):
            report(*_prefetch_progress)
        return

    failures = 0
    started, served_at_start = time.time(), _REQUESTS_SERVED
    # The one place a lot of requests go out together: find out whether
    # TestRail has raised the cap since the last download (see the pacer notes).
    _start_probe_window()

    def tick(done: int, total: int) -> None:
        _prefetch_progress[:] = [done, total]      # read by followers
        report(done, total)

    try:
        # Step 1: resolve all project IDs in parallel (skip suites that fail)
        with ThreadPoolExecutor(max_workers=min(len(suite_ids), 8)) as pool:
            pid_futures = {sid: pool.submit(resolve_project_id, sid)
                           for sid in suite_ids}
        suite_to_project: dict[int, int] = {}
        for sid, fut in pid_futures.items():
            try:
                suite_to_project[sid] = fut.result()
            except Exception as exc:                                    # noqa: BLE001
                failures += 1
                logger.warning(
                    "prefetch: could not resolve suite %s — skipping (%s)",
                    sid, str(exc)[:200])

        # Step 2: cases + sections + labels for every suite, in parallel.
        # A failure here is not fatal — these calls only warm the cache, and
        # anything missing is fetched on first use — but it IS expensive now,
        # so it is counted and it shortens the next re-warm's cooldown.
        project_ids = set(suite_to_project.values())
        n_total = len(suite_to_project) + len(suite_to_project) + len(project_ids)
        n_done = 0
        with ThreadPoolExecutor(
                max_workers=min(len(suite_ids) * 2 + len(project_ids), 16)) as pool:
            labels: dict[Future, str] = {}
            for sid, pid in suite_to_project.items():
                labels[pool.submit(fetch_cases, pid, sid)] = f"suite {sid}"
                labels[pool.submit(fetch_sections, pid, sid)] = f"sections of {sid}"
            for pid in project_ids:
                labels[pool.submit(fetch_labels, pid)] = f"labels of project {pid}"

            pending = set(labels)
            tick(0, n_total)
            while pending:
                # Poll rather than block: `as_completed` only wakes on a
                # completion, and at ~3.5s a request those are a minute or more
                # apart.  A progress box that stands still for a minute is
                # indistinguishable from one that has died.
                done, pending = wait(pending, timeout=_PROGRESS_TICK)
                for fut in done:
                    n_done += 1
                    try:
                        fut.result()
                    except Exception as exc:                            # noqa: BLE001
                        failures += 1
                        # One line, not a traceback: a rate-limited fetch is an
                        # expected outcome with a self-explanatory message, and
                        # nine 60-line stacks buried the one line that mattered.
                        logger.warning(
                            "prefetch: %s failed — will retry on first use (%s)",
                            labels[fut], str(exc)[:200])
                tick(n_done, n_total)
    finally:
        if failures:
            _warm_failures += 1
            backoff = min(_WARM_RETRY_AFTER * 2 ** (_warm_failures - 1),
                          _WARM_RETRY_MAX)
            _WARMED_AT = time.time() - _WARM_INTERVAL + backoff
            logger.warning(
                "prefetch: %d task(s) failed (%d warm-up(s) in a row) — "
                "re-warm allowed again in %.0fs",
                failures, _warm_failures, backoff)
        else:
            _warm_failures = 0
        _PREFETCH_IDLE.set()            # release every session following it
        # What the download actually achieved, in one line: the measurement
        # that showed a second account bought nothing (see the pacer notes).
        secs = max(1.0, time.time() - started)
        sent = _REQUESTS_SERVED - served_at_start
        logger.warning(
            "prefetch: %d requests in %.0fs = %.1f/min; pacer believes %s req/min",
            sent, secs, sent * 60 / secs, _effective_limit())
