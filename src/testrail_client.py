"""Thin TestRail API wrapper with pagination, retries and Streamlit caching.

TestRail v2 APIs (get_cases, get_sections, ...) return paginated envelopes:
    {"offset": 0, "limit": 250, "size": N, "_links": {"next": "...", "prev": "..."}, "cases": [...]}
When "next" is null we are done. We follow the next link (relative) until exhausted.
"""
from __future__ import annotations

import itertools
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
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


class TestRailError(RuntimeError):
    pass


# Every cached read below is persisted to disk.  A cold download is ~200
# paginated requests, so at the cap TestRail currently enforces it is measured
# in minutes, not seconds — and until this was persisted a script restart threw
# it away and paid it again, which is what "the dashboard stopped loading"
# actually was.  The payloads are lists of dicts, so they pickle cleanly.
#
# CAVEAT worth knowing: Streamlit drops `ttl` on persisted caches (it says so in
# the log on every boot), so these entries do NOT expire on their own.  The 6h
# refresh is driven instead by `app.py`'s watchdog, which clears them explicitly
# — see `_numbers_fetched_at`, which is persisted for the same reason so the
# "Updated Xm ago" label ages with the data rather than with the process.

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
# So the rate is no longer a constant we maintain.  It is, in order:
#   1. TESTRAIL_RATE_LIMIT from the secrets, when set (requests/min per user);
#   2. otherwise the pessimistic default below;
# and in BOTH cases it is lowered the moment TestRail contradicts us — the 429
# body carries the real number, so the one authority on the limit is the
# limiter itself.  We never raise it from a 429: guessing low costs minutes,
# guessing high costs the entire dashboard.
#
# Why a pacer at all: the warm-up fires up to ~80 concurrent requests.  Against
# a limiter that is an avalanche — one 429 becomes eighty, a browser refresh
# starts a second wave over the first, and nothing reaches the cache so the
# next session pays it all again.  Every request from every thread and session
# (same process) instead reserves a time slot, so the cold download is
# deterministic and produces essentially zero 429s.  Round-robin across the
# pool means consecutive slots land on consecutive accounts, which is what
# keeps each individual user under its own per-user cap.
_PACE_LOCK = threading.Lock()
_PACE_NEXT = 0.0

# What TestRail enforced on 2026-08-17.  Deliberately the pessimistic figure:
# if the cap is raised again, TESTRAIL_RATE_LIMIT is a one-line secrets edit,
# whereas a default that is too high takes the dashboard down.
_DEFAULT_LIMIT = 5
# 15% headroom.  Pacing at exactly the cap sits on the boundary, where our
# clock and TestRail's disagree by more than enough to 429 anyway.
_HEADROOM = 0.85

_LIMIT_LOCK = threading.Lock()
_limit_declared = _DEFAULT_LIMIT      # from the secrets, or the default
_limit_observed: int | None = None    # what a 429 told us; wins when lower
_pool_size = 1                        # working accounts; set by `_get_client`
_PACE_INTERVAL = 60.0 / (_DEFAULT_LIMIT * _HEADROOM)

# "API Rate Limit Exceeded - 5 per minute maximum allowed. Retry after 57s."
_LIMIT_RE = re.compile(r"(\d+)\s+per\s+minute\s+maximum\s+allowed", re.I)


def _effective_limit() -> int:
    """Requests/minute allowed on ONE account, believing the smaller claim."""
    if _limit_observed is not None:
        return min(_limit_declared, _limit_observed)
    return _limit_declared


def _repace() -> None:
    """Recompute the slot interval from (per-account limit × working accounts)."""
    global _PACE_INTERVAL
    _PACE_INTERVAL = 60.0 / max(0.1, _effective_limit() * _pool_size * _HEADROOM)


def _learn_limit(body: str) -> None:
    """Take TestRail's word for the cap — downwards only."""
    global _limit_observed
    match = _LIMIT_RE.search(body or "")
    if not match:
        return
    told = max(1, int(match.group(1)))
    with _LIMIT_LOCK:
        if _limit_observed is not None and told >= _limit_observed:
            return
        _limit_observed = told
        _repace()
    logging.getLogger(__name__).warning(
        "TestRail says the cap is %d requests/minute per account — re-pacing to "
        "%.0f/min across %d worker(s) (slot every %.1fs)",
        told, 60 / _PACE_INTERVAL, _pool_size, _PACE_INTERVAL)


def _configured_limit() -> int:
    """TESTRAIL_RATE_LIMIT from the secrets, else the default."""
    try:
        return max(1, int(st.secrets["TESTRAIL_RATE_LIMIT"]))
    except Exception:                                                   # noqa: BLE001
        return _DEFAULT_LIMIT


_STATS_LOCK = threading.Lock()
_REQUESTS_SERVED = 0


def requests_served() -> int:
    """Successful API calls this process has made.

    The loader's heartbeat.  Suite-level progress ticks once every ~80s at the
    current cap, which on a twelve-minute download is indistinguishable from a
    hang; this moves with every single request.
    """
    return _REQUESTS_SERVED


def rate_summary() -> dict[str, float | int | bool]:
    """What the pacer is actually doing — surfaced in the log and the loader.

    A wait nobody can explain reads as a hang; the same wait with "TestRail
    allows 5 requests/minute per account" next to it reads as a queue.
    """
    return {
        "limit_per_account": _effective_limit(),
        "workers": _pool_size,
        "per_minute": 60.0 / _PACE_INTERVAL,
        "slot_seconds": _PACE_INTERVAL,
        "learned": _limit_observed is not None,
    }


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
    """Block until this thread's reserved request slot arrives."""
    global _PACE_NEXT
    with _PACE_LOCK:
        now = time.time()
        wait = _PACE_NEXT - now
        _PACE_NEXT = max(now, _PACE_NEXT) + _PACE_INTERVAL
    if wait > 0:
        time.sleep(wait)


def _pace_cooldown(seconds: float) -> None:
    """Push EVERY queued slot past the window TestRail just closed.

    The thread that collected the 429 always waited politely.  The other
    seventy-nine kept firing into the same closed window — so one 429 became
    eighty, and the retries were what held the window shut.  Backing the whole
    pacer off together is what turns an avalanche into a pause.
    """
    global _PACE_NEXT
    with _PACE_LOCK:
        _PACE_NEXT = max(_PACE_NEXT, time.time() + seconds)


@dataclass(frozen=True)
class TestRailCredentials:
    base_url: str
    user: str
    api_key: str

    @classmethod
    def from_secrets(cls, suffix: str = "") -> "TestRailCredentials":
        """Read one credential set.  *suffix* selects an extra account:
        "" is TESTRAIL_USER, "_1" is TESTRAIL_USER_1, and so on."""
        try:
            url = st.secrets["TESTRAIL_URL"].rstrip("/")
            user = st.secrets[f"TESTRAIL_USER{suffix}"]
            key = st.secrets[f"TESTRAIL_API_KEY{suffix}"]
        except Exception as exc:
            raise TestRailError(
                "Missing TestRail secrets. Add TESTRAIL_URL, TESTRAIL_USER, "
                "TESTRAIL_API_KEY to .streamlit/secrets.toml or the Streamlit Cloud secrets panel."
            ) from exc
        if not str(user).strip() or not str(key).strip():
            raise TestRailError(f"Empty TestRail credentials for suffix {suffix!r}")
        return cls(base_url=url, user=user, api_key=key)


# How many extra accounts to look for.  The cap is PER USER, so each working
# account raises the ceiling by one whole allowance and the cold download
# divides by the number of them — with the cap now at 5/min that is the only
# lever left that actually scales.  Discovered, never configured: whatever is
# filled in on the day is what gets used, so adding an account is a secrets
# edit and nothing else.
_MAX_EXTRA_ACCOUNTS = 8


def _credential_sets() -> list[TestRailCredentials]:
    """Every credential set present in secrets: the base one, then _1, _2, …

    Gaps are skipped rather than treated as the end — the accounts arrive one
    at a time, and a half-filled _3 should not hide a working _4.
    """
    out: list[TestRailCredentials] = [TestRailCredentials.from_secrets()]
    seen = {(out[0].user or "").strip().lower()}
    for i in range(1, _MAX_EXTRA_ACCOUNTS + 1):
        try:
            creds = TestRailCredentials.from_secrets(f"_{i}")
        except TestRailError:
            continue
        # A duplicated user would share one rate-limit budget while we paced as
        # if it were two — the worst possible outcome, since it looks faster and
        # 429s instead.
        if (creds.user or "").strip().lower() in seen:
            continue
        seen.add(creds.user.strip().lower())
        out.append(creds)
    return out


class TestRailClient:
    """Lightweight TestRail client. Instances are cheap — reuse the underlying Session."""

    def __init__(self, creds: TestRailCredentials, timeout: int = 60,
                 extra: list[TestRailCredentials] | None = None) -> None:
        self.creds = creds
        self.timeout = timeout
        # One authenticated session per ACCOUNT, alternated request by request.
        # The rate limit is per user, so N accounts give N × the budget — and
        # round-robin per request (not per suite) is what keeps them level: the
        # suites differ in size by an order of magnitude, so splitting by suite
        # would leave one account idle while another queued.
        self._all_creds = [creds] + list(extra or [])
        self._sessions: list[requests.Session] = []
        self._rr = itertools.count()
        for c in self._all_creds:
            self._sessions.append(self._make_session(c))
        self._session = self._sessions[0]      # kept: single-session callers

    @property
    def n_accounts(self) -> int:
        return len(self._sessions)

    def _next_session(self) -> requests.Session:
        return self._sessions[next(self._rr) % len(self._sessions)]

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
        resp = self._next_session().get(self._url(endpoint), timeout=self.timeout)
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
            resp = self._next_session().get(self._url(endpoint), timeout=self.timeout)
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

        Sequential ON PURPOSE, like `get_sections` and `_get_paginated`.  This
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

    def get_case(self, case_id: int) -> dict:
        """A single test case by ID (title, refs, section, type, custom fields)."""
        return self._get(f"get_case/{case_id}")

    def get_statuses(self) -> list[dict]:
        """All result statuses, including custom ones (id ≥ 6)."""
        return self._get("get_statuses")

    # -------------------------------------------------- runs / plans / results
    def _get_paginated(self, endpoint: str, key: str, limit: int = 250) -> list[dict]:
        """Generic paginated fetch — used for runs / plans / tests / results.

        TestRail v2 returns either a bare list (older deployments) or an envelope
        ``{key: [...], _links: {next: ...}}``.  We follow ``_links.next`` until null.

        TestRail's URL convention uses `&` for all params after the endpoint path
        (the leading `?` is in the rewrite rule: index.php?/api/v2/<endpoint>).
        """
        out: list[dict] = []
        url = f"{endpoint}&limit={limit}&offset=0"
        while url:
            payload = self._get(url)
            if isinstance(payload, list):
                return payload   # Old TR — full list, no pagination envelope.
            out.extend(payload.get(key, []))
            nxt = (payload.get("_links") or {}).get("next")
            url = nxt.lstrip("/") if nxt else None
        return out

    def get_runs(self, project_id: int, is_completed: bool | None = None) -> list[dict]:
        """List runs for a project (excluding runs that belong to a plan).

        Each run dict already carries summary counts: passed_count, failed_count,
        blocked_count, untested_count, retest_count, custom_status_*_count.
        """
        endpoint = f"get_runs/{project_id}"
        if is_completed is not None:
            endpoint += f"&is_completed={1 if is_completed else 0}"
        return self._get_paginated(endpoint, key="runs")

    def get_plans(self, project_id: int, is_completed: bool | None = None) -> list[dict]:
        """List test plans for a project (each plan can contain many runs)."""
        endpoint = f"get_plans/{project_id}"
        if is_completed is not None:
            endpoint += f"&is_completed={1 if is_completed else 0}"
        return self._get_paginated(endpoint, key="plans")

    def get_plan(self, plan_id: int) -> dict:
        """Plan detail with `entries` → each entry has `runs`."""
        return self._get(f"get_plan/{plan_id}")

    def get_tests(self, run_id: int) -> list[dict]:
        """All tests in a run with their current status_id."""
        return self._get_paginated(f"get_tests/{run_id}", key="tests")

    def get_results_for_run(self, run_id: int, status_id: int | None = None) -> list[dict]:
        """All results for a run, optionally filtered by status_id (5 = failed)."""
        endpoint = f"get_results_for_run/{run_id}"
        if status_id is not None:
            endpoint += f"&status_id={status_id}"
        return self._get_paginated(endpoint, key="results")

    def get_results_for_case(self, run_id: int, case_id: int) -> list[dict]:
        """Every result the case accrued in one run (newest first per TestRail)."""
        return self._get_paginated(
            f"get_results_for_case/{run_id}/{case_id}", key="results")


# --------------------------------------------------------------------- caching
# We cache at the *function* level so Streamlit's cache key includes arguments.
# The actual TestRailClient is rebuilt per call but reuses a module-level Session.
_SESSION_CACHE: dict[str, TestRailClient] = {}
_POOL_LOCK = threading.Lock()


# (working, configured) from the last pool build.  The UI shows both when they
# differ: "3 workers" when four are configured means one is being rejected, and
# without the second number that silence looks like a slow day.
_POOL_SUMMARY: dict[str, int] = {"working": 0, "configured": 0}


def n_workers() -> int:
    """How many accounts are actually serving requests, 0 before the pool exists.

    Read by the UI: the whole point of the pool is invisible otherwise, and a
    speed-up nobody can see is a speed-up nobody trusts.
    """
    pool = _SESSION_CACHE.get("pool")
    return pool.n_accounts if pool is not None else 0


def n_accounts_configured() -> int:
    """How many credential sets were FOUND, working or not."""
    return _POOL_SUMMARY["configured"]


def ensure_pool() -> None:
    """Build the account pool now, so callers can report the real pacing.

    The warm-up's "🔌 Connecting to TestRail…" step used to connect to nothing
    — the pool was built lazily by the first fetch, several lines later, which
    meant anything printed before it described a pool of one.
    """
    _get_client()


def _account_works(creds: TestRailCredentials) -> bool:
    """One cheap authenticated call.  A credential set that cannot answer would
    otherwise fail 1/N of every request for the rest of the session, which reads
    as an intermittent TestRail fault rather than as a bad secret.

    Two deliberate choices here, both learned the hard way:

    * UNPACED.  This is exactly one request on one account, so no per-user cap
      can be breached — while paying the global slot (now ~14s) for each probe
      would spend most of a minute before the download even starts.
    * A 429 counts as WORKING.  The cap is per user, so TestRail had to
      authenticate the account before it could throttle it: a 429 proves the
      credentials are good.  Rejecting it dropped healthy accounts whenever the
      window happened to be closed at startup — which is the likeliest reason a
      fourth configured account once showed up as "3 of 4 workers".
    """
    try:
        probe = TestRailClient(creds)
        resp = probe._sessions[0].get(probe._url("get_priorities"), timeout=30)
        if resp.status_code == 429:
            _learn_limit(resp.text)
            return True
        if resp.ok:
            return True
        reason = f"{resp.status_code}: {resp.text[:120]}"
    except Exception as exc:                                            # noqa: BLE001
        reason = str(exc)[:120]
    logging.getLogger(__name__).warning(
        "TestRail account %s unusable, skipping: %s", creds.user, reason)
    return False


def _get_client() -> TestRailClient:
    """The shared client, holding one session per WORKING account.

    Built once per process: the probe costs one request per account, and the
    pool size decides the pacing for everything that follows.
    """
    global _pool_size, _limit_declared
    key = "pool"
    cached = _SESSION_CACHE.get(key)
    if cached is not None:
        return cached
    # Double-checked: the warm-up submits up to 8 suite workers at once and they
    # all arrive here before the pool exists.  Unguarded, each one probed EVERY
    # account — 24 wasted requests on a pool of three, paced at the
    # single-account rate because the pacer had not been widened yet, which is
    # roughly half a minute of the speed-up spent before any real work started.
    with _POOL_LOCK:
        cached = _SESSION_CACHE.get(key)
        if cached is not None:
            return cached
        candidates = _credential_sets()
        # Probed in parallel: one request each, on distinct accounts, so the
        # whole pool costs a single round trip instead of one per account.
        with ThreadPoolExecutor(max_workers=max(1, len(candidates))) as pool:
            verdicts = list(pool.map(_account_works, candidates))
        working = [c for c, ok in zip(candidates, verdicts) if ok]
        if not working:
            # Same failure as before multi-account: no usable credentials at all.
            raise TestRailError(
                "No usable TestRail credentials — check TESTRAIL_USER / "
                "TESTRAIL_API_KEY (and any _1.._N variants) in the secrets."
            )
        _POOL_SUMMARY.update(working=len(working), configured=len(candidates))
        _pool_size = len(working)
        _limit_declared = _configured_limit()
        _repace()
        logging.getLogger(__name__).warning(
            "TestRail: %d/%d account(s) usable, cap %d req/min each%s → "
            "%.0f requests/min total (slot every %.1fs)",
            len(working), len(candidates), _effective_limit(),
            " (observed from a 429)" if _limit_observed is not None else "",
            60 / _PACE_INTERVAL, _PACE_INTERVAL)
        _SESSION_CACHE[key] = TestRailClient(working[0], extra=working[1:])
        return _SESSION_CACHE[key]


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def fetch_case_fields() -> list[dict]:
    return _get_client().get_case_fields()


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def fetch_case_types() -> list[dict]:
    return _get_client().get_case_types()


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def fetch_priorities() -> list[dict]:
    return _get_client().get_priorities()


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def fetch_suite(suite_id: int) -> dict:
    return _get_client().get_suite(suite_id)


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
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


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def _fetch_cases_cached(project_id: int, suite_id: int) -> list[dict]:
    return [_slim_case(c) for c in _get_client().get_cases(project_id, suite_id)]


def fetch_cases(project_id: int, suite_id: int) -> list[dict]:
    with _sf_lock(("cases", project_id, suite_id)):
        return _fetch_cases_cached(project_id, suite_id)


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def _fetch_labels_cached(project_id: int) -> dict[int, str]:
    """Return {label_id: label_name} for the given project."""
    raw = _get_client().get_labels(project_id)
    return {int(lbl["id"]): lbl.get("title", lbl.get("name", "")) for lbl in raw}


def fetch_labels(project_id: int) -> dict[int, str]:
    with _sf_lock(("labels", project_id)):
        return _fetch_labels_cached(project_id)


# ── runs / plans / results ───────────────────────────────────────────────────
# Split by whether the thing being asked about can still change.
#
# A COMPLETED run or plan is immutable — TestRail cannot alter it — so it gets
# the same long, persisted TTL `fetch_tests` has always used.  Only the ACTIVE
# queries need the short one.
#
# This used to be a single 10-minute TTL for both, which was affordable at 180
# requests/minute.  At 5 it is not arithmetic that works: the Runs/Stability
# view costs roughly 25 requests per BU, which at the current pace is ~90
# seconds — so a 10-minute TTL meant closed history nobody could have changed
# was re-downloaded all day, out of the same budget the coverage data needs.
@st.cache_data(show_spinner=False, ttl=600)
def _fetch_runs_live(project_id: int, is_completed: bool | None) -> list[dict]:
    return _get_client().get_runs(project_id, is_completed=is_completed)


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def _fetch_runs_closed(project_id: int) -> list[dict]:
    return _get_client().get_runs(project_id, is_completed=True)


def fetch_runs(project_id: int, is_completed: bool | None = None) -> list[dict]:
    if is_completed is True:
        return _fetch_runs_closed(project_id)
    return _fetch_runs_live(project_id, is_completed)


@st.cache_data(show_spinner=False, ttl=600)
def _fetch_plans_live(project_id: int, is_completed: bool | None) -> list[dict]:
    return _get_client().get_plans(project_id, is_completed=is_completed)


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def _fetch_plans_closed(project_id: int) -> list[dict]:
    return _get_client().get_plans(project_id, is_completed=True)


def fetch_plans(project_id: int, is_completed: bool | None = None) -> list[dict]:
    if is_completed is True:
        return _fetch_plans_closed(project_id)
    return _fetch_plans_live(project_id, is_completed)


@st.cache_data(show_spinner=False, ttl=600)
def fetch_plan(plan_id: int) -> dict:
    """Detail of an ACTIVE plan — its runs and counts are still moving."""
    return _get_client().get_plan(plan_id)


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def fetch_plan_closed(plan_id: int) -> dict:
    """Detail of a COMPLETED plan.  Use ONLY for plans TestRail reports as
    completed — same immutability contract as `fetch_tests`."""
    return _get_client().get_plan(plan_id)


# Completed-run data is immutable → long TTL (6h).  Use ONLY for completed runs.
@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def fetch_tests(run_id: int) -> list[dict]:
    return _get_client().get_tests(run_id)


# Same call, short TTL — for ACTIVE runs, whose tests/statuses keep changing.
@st.cache_data(show_spinner=False, ttl=600)
def fetch_tests_fresh(run_id: int) -> list[dict]:
    return _get_client().get_tests(run_id)


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def fetch_statuses() -> dict[int, str]:
    """{status_id: display label} incl. custom statuses (id ≥ 6)."""
    return {
        int(s["id"]): (s.get("label") or s.get("name") or f"Status {s['id']}")
        for s in _get_client().get_statuses()
    }


@st.cache_data(show_spinner=False, ttl=600)
def fetch_failed_results(run_id: int) -> list[dict]:
    """Failed results only (status_id=5) — used for bug/defect extraction."""
    return _get_client().get_results_for_run(run_id, status_id=5)


@st.cache_data(show_spinner=False, ttl=21600, persist="disk")
def fetch_case(case_id: int) -> dict:
    """A single case by ID — used by the Runs tab's in-depth analysis."""
    return _get_client().get_case(case_id)


@st.cache_data(show_spinner=False, ttl=600)
def fetch_results_for_case(run_id: int, case_id: int) -> list[dict]:
    """Result history of one case within one run."""
    return _get_client().get_results_for_case(run_id, case_id)


def resolve_project_id(suite_id: int) -> int:
    """Get the project_id that owns a given suite (needed for get_cases)."""
    suite = fetch_suite(suite_id)
    return int(suite["project_id"])


def clear_all_caches() -> None:
    for fn in (fetch_case_fields, fetch_case_types, fetch_priorities,
               fetch_suite, _fetch_sections_cached, _fetch_cases_cached,
               _fetch_labels_cached,
               _fetch_runs_live, _fetch_runs_closed,
               _fetch_plans_live, _fetch_plans_closed,
               fetch_plan, fetch_plan_closed, fetch_tests, fetch_tests_fresh,
               fetch_failed_results, fetch_case, fetch_results_for_case,
               fetch_statuses):
        fn.clear()


# ----------------------------------------------------------------- startup pre-warm
# Wall-clock of the last pre-warm.  Slightly shorter than the data TTL (6h)
# so the parallel pre-warm kicks in again just before the cache entries lapse —
# the old boolean flag never reset, leaving every post-TTL refresh un-warmed.
_WARMED_AT = 0.0
_WARM_INTERVAL = 21300.0   # data TTL (6h) minus 5 min — re-warm just before expiry


# How long a failed warm-up holds the lock before another session may retry.
# NOT the full interval: the caches are empty when it fails, so claiming the
# next six hours turns one bad rate-limit window into an afternoon of lazy,
# serial fetches — which is exactly how a single 429 storm used to become a
# dashboard that stayed broken long after TestRail had recovered.
_WARM_RETRY_AFTER = 120.0

# How often the download reports in.  Suite completions are ~80s apart at the
# current cap; this ticks the counter in between so the box always moves.
_PROGRESS_TICK = 2.0


def prefetch_all_suites(suite_ids: list[int], on_progress=None) -> None:
    """Pre-warm fetch_cases + fetch_sections + fetch_labels for every suite.

    Fault-tolerant per suite: one deleted/renamed suite must not blank the
    whole dashboard — its failure is logged and skipped here, and if the suite
    genuinely matters, `evaluate_rules` will surface a visible error for it.

    *on_progress* is an optional callback(done, total, requests) fired at least
    every couple of seconds — from THIS thread, never from a worker, so the UI
    call is always made where Streamlit expects it.
    """
    global _WARMED_AT
    if time.time() - _WARMED_AT < _WARM_INTERVAL:
        return
    _WARMED_AT = time.time()   # claimed upfront so concurrent sessions don't re-warm
    failures = 0

    def tick(done: int, total: int) -> None:
        if not on_progress:
            return
        try:
            on_progress(done, total, _REQUESTS_SERVED)
        except BaseException:                                           # noqa: BLE001
            # See evaluate_rules' progress hook: a killed session's UI callback
            # must not abort a download the other sessions are waiting on.
            pass

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
                logging.getLogger(__name__).warning(
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
                        logging.getLogger(__name__).warning(
                            "prefetch: %s failed — will retry on first use (%s)",
                            labels[fut], str(exc)[:200])
                tick(n_done, n_total)
    finally:
        if failures:
            _WARMED_AT = time.time() - _WARM_INTERVAL + _WARM_RETRY_AFTER
            logging.getLogger(__name__).warning(
                "prefetch: %d task(s) failed — re-warm allowed again in %.0fs",
                failures, _WARM_RETRY_AFTER)
