"""Thin TestRail API wrapper with pagination, retries and Streamlit caching.

TestRail v2 APIs (get_cases, get_sections, ...) return paginated envelopes:
    {"offset": 0, "limit": 250, "size": N, "_links": {"next": "...", "prev": "..."}, "cases": [...]}
When "next" is null we are done. We follow the next link (relative) until exhausted.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
import streamlit as st
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
            # Rate limit — honour Retry-After and retry once manually
            wait = int(resp.headers.get("Retry-After", "5"))
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


def resolve_project_id(suite_id: int) -> int:
    """Get the project_id that owns a given suite (needed for get_cases)."""
    suite = fetch_suite(suite_id)
    return int(suite["project_id"])


def clear_all_caches() -> None:
    for fn in (fetch_case_fields, fetch_case_types, fetch_priorities,
               fetch_suite, fetch_sections, fetch_cases):
        fn.clear()


# ----------------------------------------------------------------- startup pre-warm
_WARMED = False   # module-level flag — runs once per process, not per user session


def prefetch_all_suites(suite_ids: list[int]) -> None:
    """Pre-warm fetch_cases + fetch_sections caches for every known suite.

    Called once at app startup.  Subsequent BU clicks only need Python
    processing (rule matching), not API calls.
    """
    global _WARMED
    if _WARMED:
        return
    _WARMED = True

    # Step 1: resolve all project IDs in parallel
    with ThreadPoolExecutor(max_workers=min(len(suite_ids), 8)) as pool:
        pid_futures = {sid: pool.submit(resolve_project_id, sid) for sid in suite_ids}
    suite_to_project = {sid: f.result() for sid, f in pid_futures.items()}

    # Step 2: fetch cases + sections for all suites in parallel
    with ThreadPoolExecutor(max_workers=min(len(suite_ids) * 2, 12)) as pool:
        for sid, pid in suite_to_project.items():
            pool.submit(fetch_cases, pid, sid)
            pool.submit(fetch_sections, pid, sid)
