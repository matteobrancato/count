"""Thin TestRail API wrapper with pagination, retries and Streamlit caching.

TestRail v2 APIs (get_cases, get_sections, ...) return paginated envelopes:
    {"offset": 0, "limit": 250, "size": N, "_links": {"next": "...", "prev": "..."}, "cases": [...]}
When "next" is null we are done. We follow the next link (relative) until exhausted.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
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


@dataclass(frozen=True)
class TestRailCredentials:
    base_url: str
    user: str
    api_key: str

    @classmethod
    def from_secrets(cls) -> "TestRailCredentials":
        try:
            url = st.secrets["TESTRAIL_URL"].rstrip("/")
            user = st.secrets["TESTRAIL_USER"]
            key = st.secrets["TESTRAIL_API_KEY"]
        except Exception as exc:
            raise TestRailError(
                "Missing TestRail secrets. Add TESTRAIL_URL, TESTRAIL_USER, "
                "TESTRAIL_API_KEY to .streamlit/secrets.toml or the Streamlit Cloud secrets panel."
            ) from exc
        return cls(base_url=url, user=user, api_key=key)


class TestRailClient:
    """Lightweight TestRail client. Instances are cheap — reuse the underlying Session."""

    def __init__(self, creds: TestRailCredentials, timeout: int = 60) -> None:
        self.creds = creds
        self.timeout = timeout
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth(creds.user, creds.api_key)
        self._session.headers.update({"Content-Type": "application/json"})
        # Big connection pool — the cold-start warm-up fires many parallel
        # requests (16 suite workers × up to 5 pagination workers each ≈ 80
        # peak).  The default urllib3 pool is only 10 connections, so without
        # this the parallel fetches silently queue behind 10 sockets.  maxsize
        # is a cap, not a preallocation, so oversizing is free — 96 covers the
        # warm-up peak without connection churn (discard/reopen).
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=96, max_retries=0)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

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
        resp = self._session.get(self._url(endpoint), timeout=self.timeout)
        if resp.status_code == 429:
            # Rate limit — honour Retry-After (seconds form; RFC 7231 also
            # allows an HTTP-date, which int() can't parse) and retry once.
            # Capped so a hostile/buggy header can't stall a worker thread.
            try:
                wait = min(int(resp.headers.get("Retry-After", "5")), 30)
            except ValueError:
                wait = 5
            time.sleep(wait)
            resp = self._session.get(self._url(endpoint), timeout=self.timeout)
        if not resp.ok:
            raise TestRailError(f"GET {endpoint} → {resp.status_code}: {resp.text[:300]}")
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
        """Fetch all cases with parallel pagination (N pages at a time)."""
        base = f"get_cases/{project_id}&suite_id={suite_id}&limit={limit}"

        # Page 0 — always sequential so we know if there is more
        first = self._get(f"{base}&offset=0")
        if isinstance(first, list):
            return first
        cases: list[dict] = list(first.get("cases", []))
        if first.get("size", 0) < limit:
            return cases  # fits in a single page

        # Fetch remaining pages in parallel batches of BATCH_SIZE
        BATCH_SIZE = 5
        offset = limit
        while True:
            offsets = list(range(offset, offset + BATCH_SIZE * limit, limit))
            with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
                futures = [(o, pool.submit(self._get, f"{base}&offset={o}"))
                           for o in offsets]
            done = False
            for o, fut in futures:            # already in order
                data = fut.result()
                page = data.get("cases", []) if not isinstance(data, list) else data
                cases.extend(page)
                if len(page) < limit:        # last page found → stop
                    done = True
                    break
            if done:
                break
            offset += BATCH_SIZE * limit
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


def _get_client() -> TestRailClient:
    creds = TestRailCredentials.from_secrets()
    key = f"{creds.base_url}|{creds.user}"
    if key not in _SESSION_CACHE:
        _SESSION_CACHE[key] = TestRailClient(creds)
    return _SESSION_CACHE[key]


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_case_fields() -> list[dict]:
    return _get_client().get_case_fields()


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_case_types() -> list[dict]:
    return _get_client().get_case_types()


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_priorities() -> list[dict]:
    return _get_client().get_priorities()


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_suite(suite_id: int) -> dict:
    return _get_client().get_suite(suite_id)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_sections(project_id: int, suite_id: int) -> list[dict]:
    return _get_client().get_sections(project_id, suite_id)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_cases(project_id: int, suite_id: int) -> list[dict]:
    return _get_client().get_cases(project_id, suite_id)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_labels(project_id: int) -> dict[int, str]:
    """Return {label_id: label_name} for the given project."""
    raw = _get_client().get_labels(project_id)
    return {int(lbl["id"]): lbl.get("title", lbl.get("name", "")) for lbl in raw}


# ── runs / plans / results — shorter TTL because active state changes often ──
@st.cache_data(show_spinner=False, ttl=600)
def fetch_runs(project_id: int, is_completed: bool | None = None) -> list[dict]:
    return _get_client().get_runs(project_id, is_completed=is_completed)


@st.cache_data(show_spinner=False, ttl=600)
def fetch_plans(project_id: int, is_completed: bool | None = None) -> list[dict]:
    return _get_client().get_plans(project_id, is_completed=is_completed)


@st.cache_data(show_spinner=False, ttl=600)
def fetch_plan(plan_id: int) -> dict:
    return _get_client().get_plan(plan_id)


# Completed-run data is immutable → long TTL (6h).  Use ONLY for completed runs.
@st.cache_data(show_spinner=False, ttl=21600)
def fetch_tests(run_id: int) -> list[dict]:
    return _get_client().get_tests(run_id)


# Same call, short TTL — for ACTIVE runs, whose tests/statuses keep changing.
@st.cache_data(show_spinner=False, ttl=600)
def fetch_tests_fresh(run_id: int) -> list[dict]:
    return _get_client().get_tests(run_id)


@st.cache_data(show_spinner=False, ttl=3600)
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


@st.cache_data(show_spinner=False, ttl=3600)
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
               fetch_suite, fetch_sections, fetch_cases, fetch_labels,
               fetch_runs, fetch_plans, fetch_plan, fetch_tests, fetch_tests_fresh,
               fetch_failed_results, fetch_case, fetch_results_for_case,
               fetch_statuses):
        fn.clear()


# ----------------------------------------------------------------- startup pre-warm
# Wall-clock of the last pre-warm.  Slightly shorter than the data TTL (3600s)
# so the parallel pre-warm kicks in again just before the cache entries lapse —
# the old boolean flag never reset, leaving every post-TTL refresh un-warmed.
_WARMED_AT = 0.0
_WARM_INTERVAL = 3300.0


def prefetch_all_suites(suite_ids: list[int]) -> None:
    """Pre-warm fetch_cases + fetch_sections caches for every known suite.

    Fault-tolerant per suite: one deleted/renamed suite must not blank the
    whole dashboard — its failure is logged and skipped here, and if the suite
    genuinely matters, `evaluate_rules` will surface a visible error for it.
    """
    global _WARMED_AT
    if time.time() - _WARMED_AT < _WARM_INTERVAL:
        return
    _WARMED_AT = time.time()   # set upfront so concurrent sessions don't re-warm

    # Step 1: resolve all project IDs in parallel (skip suites that fail)
    with ThreadPoolExecutor(max_workers=min(len(suite_ids), 8)) as pool:
        pid_futures = {sid: pool.submit(resolve_project_id, sid) for sid in suite_ids}
    suite_to_project: dict[int, int] = {}
    for sid, fut in pid_futures.items():
        try:
            suite_to_project[sid] = fut.result()
        except Exception:                                               # noqa: BLE001
            logging.getLogger(__name__).exception(
                "prefetch: could not resolve suite %s — skipping", sid)

    # Step 2: fetch cases + sections + labels for all suites in parallel.
    # Failures here are harmless: these calls only warm the cache, and any
    # suite that failed will simply be fetched (with retries) on first use.
    project_ids = set(suite_to_project.values())
    with ThreadPoolExecutor(max_workers=min(len(suite_ids) * 2 + len(project_ids), 16)) as pool:
        for sid, pid in suite_to_project.items():
            pool.submit(fetch_cases, pid, sid)
            pool.submit(fetch_sections, pid, sid)
        for pid in project_ids:
            pool.submit(fetch_labels, pid)
