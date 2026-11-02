"""Martech: the Testim automation BACKLOG of the MarTech Connect project.

A second TestRail instance (aswatson.testrail.io) with its own account, and ONE
plan, "Testim Automation BACKLOG".  There are no labels and no fields to read:
the plan IS the backlog.  Each run is a Business Unit's country, each test a
case to automate there, and the test's status says how far it has got.  The
plan's own description gives four of the statuses a meaning —

    Passed = merged · Accepted fail = PR raised · Failed = query raised to MQA
    · N/A = not applicable

— and the rest (To be tested, In Progress, Blocked, On hold, …) are shown under
TestRail's own names rather than under a meaning nobody wrote down.

Nothing here touches the main TestRail pipeline.  The instance, the account and
the rate limit are different, so this module has its own session and its own
pacing: a slow answer here can never hold up the dashboard's twelve-minute
download, and the download can never hold this up.  Nothing is fetched at start
up either — the plan is read when somebody opens Martech, one request, and a BU's
tests when somebody opens that BU.
"""
from __future__ import annotations

import functools
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import requests
import streamlit as st

from .freshness import DAY_TTL

logger = logging.getLogger(__name__)

# The plan the backlog is read from, and the instance it lives on.  Neither is a
# secret, so both are defaults that the secrets may override.
PLAN_ID = 3721
DEFAULT_URL = "https://aswatson.testrail.io"

# The secrets (see `credentials`).  Only the key is required: the account is the
# same person as the main TestRail one, so the user falls back to TESTRAIL_USER.
# Each has two spellings, the first being the documented one: the second follows
# the main account's own naming (TESTRAIL_API_KEY, TESTRAIL_API_KEY_1, ...), which
# is what people reach for first.
SECRET_KEYS = ("MARTECH_TESTRAIL_API_KEY", "TESTRAIL_API_KEY_MARTECH")
SECRET_USERS = ("MARTECH_TESTRAIL_USER", "TESTRAIL_USER_MARTECH")
SECRET_URLS = ("MARTECH_TESTRAIL_URL", "TESTRAIL_URL_MARTECH")
SECRET_PLAN = "MARTECH_PLAN_ID"


class MartechError(RuntimeError):
    """Martech data could not be read.  The message is meant to be shown."""


# ── configuration ─────────────────────────────────────────────────────────────
def _secret(name: str) -> str:
    try:
        value = st.secrets.get(name)
    except Exception:                                                   # noqa: BLE001
        return ""          # no secrets file at all: the same as "not set"
    return str(value).strip() if value is not None else ""


def _first_secret(names: tuple[str, ...]) -> str:
    """The first of *names* that is set."""
    return next((v for v in map(_secret, names) if v), "")


def configured() -> bool:
    """True once the API key is in the secrets.  Martech appears in the scope
    switch only then: an option that can only say "not set up" is clutter for
    everybody who reads the dashboard."""
    return bool(_first_secret(SECRET_KEYS))


@dataclass(frozen=True)
class Credentials:
    base_url: str
    user: str
    api_key: str = field(repr=False)        # never printed, even by a traceback


def credentials() -> Credentials:
    key = _first_secret(SECRET_KEYS)
    if not key:
        raise MartechError(f"{SECRET_KEYS[0]} is not set in the app secrets.")
    user = _first_secret(SECRET_USERS) or _secret("TESTRAIL_USER")
    if not user:
        raise MartechError(f"Neither {SECRET_USERS[0]} nor TESTRAIL_USER is set in the "
                           f"app secrets.")
    return Credentials((_first_secret(SECRET_URLS) or DEFAULT_URL).rstrip("/"), user, key)


def plan_id() -> int:
    raw = _secret(SECRET_PLAN)
    try:
        return int(raw) if raw else PLAN_ID
    except ValueError:
        raise MartechError(f"{SECRET_PLAN} must be a number, not {raw!r}.") from None


# ── the client: own session, own pacing ───────────────────────────────────────
# Sequential and gentle.  The limit on this instance is not known, so the pace
# starts at about three requests a second and follows whatever a 429 says.  It
# shares nothing with `testrail_client`, whose pacer is tuned to a cap of five a
# minute on the other instance.
_PACE_LOCK = threading.Lock()
_next_at = 0.0
_interval = 0.35
_LIMIT_RE = re.compile(r"(\d+)\s+per\s+minute", re.IGNORECASE)
_ATTEMPTS = 4


@functools.lru_cache(maxsize=4)
def _session(user: str, api_key: str) -> requests.Session:
    session = requests.Session()
    session.auth = (user, api_key)
    session.headers.update({"Content-Type": "application/json"})
    return session


def _wait_turn() -> None:
    global _next_at
    with _PACE_LOCK:
        now = time.monotonic()
        wait = _next_at - now
        _next_at = max(now, _next_at) + _interval
    if wait > 0:
        time.sleep(wait)


def _rate_limited(resp: requests.Response) -> float:
    """Slow down to what the 429 says and return how long to wait before the
    retry.  The body names the cap ("5 per minute maximum allowed"); a body that
    does not simply doubles the interval, so a wording change still converges."""
    global _interval
    match = _LIMIT_RE.search(resp.text or "")
    with _PACE_LOCK:
        if match:
            _interval = max(_interval, 60.0 / max(1.0, int(match.group(1)) * 0.8))
        else:
            _interval = min(_interval * 2, 30.0)
    try:
        return min(float(resp.headers.get("Retry-After", "10")), 60.0)
    except ValueError:
        return 10.0


def _get(endpoint: str) -> Any:
    creds = credentials()
    url = f"{creds.base_url}/index.php?/api/v2/{endpoint}"
    what = endpoint.split("&")[0]
    for attempt in range(_ATTEMPTS):
        _wait_turn()
        try:
            resp = _session(creds.user, creds.api_key).get(url, timeout=60)
        except requests.RequestException as exc:
            if attempt == _ATTEMPTS - 1:
                raise MartechError(
                    f"TestRail ({creds.base_url}) is not reachable "
                    f"({type(exc).__name__}).") from exc
            time.sleep(1 + attempt)
            continue
        if resp.status_code == 429:
            time.sleep(_rate_limited(resp))
            continue
        if resp.status_code in (401, 403):
            raise MartechError(
                f"TestRail refused the Martech credentials (HTTP {resp.status_code}). "
                f"{SECRET_KEYS[0]} must be an API key generated on {creds.base_url} — each "
                f"TestRail instance has its own — for the user {creds.user}.")
        if not resp.ok:
            raise MartechError(f"TestRail answered {resp.status_code} to {what}: "
                               f"{(resp.text or '')[:160]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise MartechError(f"TestRail's answer to {what} was not JSON.") from exc
    raise MartechError("TestRail kept refusing the requests (rate limit); try again in "
                       "a few minutes.")


# Downloaded data: plain @st.cache_data, kept across a deploy and cleared by the
# daily rollover or ↻ (`freshness.clear_everything` clears every cache).  A
# failure raises, so it is never cached for the day.
@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def fetch_statuses() -> dict[int, str]:
    """{status id: label} as this instance names them."""
    return {int(s["id"]): str(s.get("label") or s.get("name") or s["id"])
            for s in _get("get_statuses")}


@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def fetch_plan() -> dict:
    """The backlog plan and the moment it was read: {"plan": …, "read_at": …,
    "base_url": …}.  One request."""
    plan = _get(f"get_plan/{plan_id()}")
    return {"plan": plan, "read_at": time.time(), "base_url": credentials().base_url}


@st.cache_data(ttl=DAY_TTL, show_spinner=False)
def fetch_run_tests(run_id: int) -> list[dict]:
    """The tests of one run — case, title, status — following TestRail's own
    pagination."""
    out: list[dict] = []
    endpoint = f"get_tests/{int(run_id)}&limit=250"
    while endpoint:
        payload = _get(endpoint)
        page = payload.get("tests", []) if isinstance(payload, dict) else payload
        out += [{"test_id": t["id"], "case_id": t["case_id"],
                 "title": t.get("title") or "", "status_id": t["status_id"]}
                for t in page]
        nxt = (payload.get("_links") or {}).get("next") if isinstance(payload, dict) else None
        endpoint = nxt.lstrip("/").removeprefix("api/v2/") if (nxt and page) else None
    return out


# ── the plan's shape ──────────────────────────────────────────────────────────
# TestRail's counts in a plan or run object, and the status each one counts.
# 1–5 are the system statuses; the seven custom ones are 6–12 in order.
_COUNT_KEYS = {"passed_count": 1, "blocked_count": 2, "untested_count": 3,
               "retest_count": 4, "failed_count": 5,
               **{f"custom_status{n}_count": 5 + n for n in range(1, 8)}}

PASSED = 1
# What a status does for the backlog.  By LABEL, not by id: ids are this
# instance's, and a status renamed in TestRail should follow its name.
_NOT_APPLICABLE_LABELS = {"n/a", "na", "not applicable"}
# Statuses still to be worked, in the order the work flows.  Anything TestRail
# adds later lands after them, by id, so it is shown rather than dropped.
_OPEN_ORDER = (3, 9, 4, 5, 8, 10, 2, 6)

# A run is "KV NL", "IPXL BE", "WTR": the first word names the BU, and the
# Marionnaud countries are their own tokens (MFR, MCH, …).
_BU_OF_FIRST_WORD = {
    "ipxl": "ICI Paris XL", "kv": "Kruidvat", "tkp": "Trekpleister",
    "tp": "Trekpleister", "trekpleister": "Trekpleister", "wtr": "Watsons Turkey",
    "drg": "Drogas", "sd": "Superdrug", "sv": "Savers", "tps": "The Perfume Shop",
    **dict.fromkeys(("mrn", "mfr", "mch", "mat", "mcz", "mro", "mit", "msk", "mhu"),
                    "Marionnaud"),
}
# The BUs this module knows about, for the scope's BU list before anything is read.
KNOWN_BUS = tuple(dict.fromkeys(_BU_OF_FIRST_WORD.values()))


def bu_of_run(run_name: str) -> str:
    """The Business Unit a run belongs to.  A run this module does not know is
    its own row, named as in the plan: a test is never dropped for want of a
    mapping."""
    words = (run_name or "").split()
    return _BU_OF_FIRST_WORD.get(words[0].lower(), run_name) if words else run_name


@dataclass(frozen=True)
class Run:
    run_id: int
    name: str
    bu: str
    counts: dict[int, int]            # status id -> tests (zero counts left out)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def runs_in_plan(plan: dict) -> list[Run]:
    """Every run of every entry of the plan, with its status counts."""
    out: list[Run] = []
    for entry in plan.get("entries", []):
        for run in entry.get("runs", []):
            name = str(run.get("name") or entry.get("name") or run.get("id"))
            counts = {sid: int(run.get(key) or 0) for key, sid in _COUNT_KEYS.items()}
            out.append(Run(int(run["id"]), name, bu_of_run(name),
                           {sid: n for sid, n in counts.items() if n}))
    return out


def label(statuses: dict[int, str], status_id: int) -> str:
    return statuses.get(status_id, f"Status {status_id}")


def _is_not_applicable(statuses: dict[int, str], status_id: int) -> bool:
    return label(statuses, status_id).strip().lower() in _NOT_APPLICABLE_LABELS


def open_statuses(runs: list[Run], statuses: dict[int, str]) -> list[int]:
    """The statuses that carry work, which occur in *runs*, in flow order.  A
    status nobody uses is not a column."""
    present = {sid for r in runs for sid in r.counts
               if sid != PASSED and not _is_not_applicable(statuses, sid)}
    ordered = [sid for sid in _OPEN_ORDER if sid in present]
    return ordered + sorted(present - set(ordered))


def _figures(runs: list[Run], statuses: dict[int, str]) -> dict[str, int]:
    total = sum(r.total for r in runs)
    merged = sum(r.counts.get(PASSED, 0) for r in runs)
    na = sum(n for r in runs for sid, n in r.counts.items()
             if _is_not_applicable(statuses, sid))
    return {"Total": total, "Merged": merged, "Backlog": total - merged - na,
            "Not Applicable": na}


def progress(figures: dict[str, int]) -> float:
    """Merged over what is left to merge once the not-applicable ones are out —
    the same shape as Coverage vs Automatable.  Not a coverage: this is the share
    of a backlog, and it is never coloured against the 80% target."""
    scope = figures["Total"] - figures["Not Applicable"]
    return round(figures["Merged"] / scope * 100, 1) if scope else 0.0


def _frame(groups: dict[str, list[Run]], statuses: dict[int, str], key: str) -> pd.DataFrame:
    every = [r for rs in groups.values() for r in rs]
    cols = open_statuses(every, statuses)
    rows = []
    for name, rs in groups.items():
        f = _figures(rs, statuses)
        rows.append({key: name, "Runs": len(rs), **f,
                     **{label(statuses, sid): sum(r.counts.get(sid, 0) for r in rs)
                        for sid in cols},
                     "Progress %": progress(f)})
    return pd.DataFrame(rows)


def summary(runs: list[Run], statuses: dict[int, str],
            order: list[str] | None = None) -> pd.DataFrame:
    """One row per Business Unit: its tests, how many are merged, the backlog,
    one column per status that occurs, and the progress.  *order* ranks the BUs
    the way the dashboard lists them; any BU not in it follows alphabetically."""
    groups: dict[str, list[Run]] = {}
    for r in runs:
        groups.setdefault(r.bu, []).append(r)
    rank = {b: i for i, b in enumerate(order or [])}
    names = sorted(groups, key=lambda b: (rank.get(b, len(rank)), b))
    return _frame({b: groups[b] for b in names}, statuses, "BU")


def runs_summary(runs: list[Run], statuses: dict[int, str], bu: str) -> pd.DataFrame:
    """One row per run of *bu*, for its detail view."""
    mine = [r for r in runs if r.bu == bu]
    return _frame({r.name: [r] for r in mine}, statuses, "Run").drop(columns="Runs")


def bu_figures(runs: list[Run], statuses: dict[int, str], bu: str) -> dict[str, int]:
    return _figures([r for r in runs if r.bu == bu], statuses)


def tests_frame(runs: list[Run], tests_by_run: dict[int, list[dict]],
                statuses: dict[int, str], base_url: str) -> pd.DataFrame:
    """Every test of *runs*: case (as a TestRail link), title, run, status."""
    rows = [{"Case": f"{base_url}/index.php?/cases/view/{t['case_id']}",
             "Title": t["title"], "Run": r.name,
             "Status": label(statuses, t["status_id"])}
            for r in runs for t in tests_by_run.get(r.run_id, [])]
    return pd.DataFrame(rows, columns=["Case", "Title", "Run", "Status"])


def plan_url(base_url: str) -> str:
    return f"{base_url}/index.php?/plans/view/{plan_id()}"
