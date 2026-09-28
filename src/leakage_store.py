"""Where the AI's proposals and the Key QA's decisions are kept.

Decided with Matteo on 2026-09-28: no permanent archive for now.  This store
lives in the app process: shared by every visitor, kept across reruns and
days, and LOST when the app restarts or redeploys.  The tab says so next to
the Save button, so nobody reviews forty incidents believing it is on record.

What it keeps, per Jira incident:
  * the AI proposal, never overwritten by a review — only by a re-analysis,
    and the proposal it replaces stays in the history;
  * every review as an event: who, when, what the final values became, the
    values before, the comment, and whether it confirmed or changed the AI.

`Store` is the whole interface.  A permanent backend (a private repository,
a database, Jira fields) implements the same six methods and replaces
`MemoryStore` in `store()` — nothing else changes.
"""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import streamlit as st

PENDING = "Pending"
CONFIRMED = "Confirmed"
CHANGED = "Changed"
INVESTIGATE = "Needs investigation"
REVIEW_STATUSES = (PENDING, CONFIRMED, CHANGED, INVESTIGATE)

# The values a Key QA can set, and that the AI proposes.
FINAL_FIELDS = ("uat_detectability", "category", "testrail_case")


@dataclass
class Event:
    at: str
    reviewer: str
    status: str
    final: dict
    before: dict
    comment: str = ""


@dataclass
class Record:
    ai: dict | None = None                       # the current AI proposal
    ai_model: str = ""
    ai_at: str = ""
    ai_input: str = ""                           # hash of what the AI read
    past_ai: list[dict] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    @property
    def final(self) -> dict:
        """The reviewed values if any, else the AI's proposal."""
        if self.events:
            return dict(self.events[-1].final)
        ai = self.ai or {}
        return {k: ai.get(k, "") for k in FINAL_FIELDS}

    @property
    def status(self) -> str:
        return self.events[-1].status if self.events else PENDING

    @property
    def reviewer(self) -> str:
        return self.events[-1].reviewer if self.events else ""

    @property
    def reviewed_at(self) -> str:
        return self.events[-1].at if self.events else ""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class Store(Protocol):
    def get(self, project: str, key: str) -> Record: ...
    def put_ai(self, project: str, key: str, ai: dict, model: str, input_hash: str) -> None: ...
    def review(self, project: str, key: str, reviewer: str, status: str,
               final: dict, comment: str = "") -> None: ...
    def attempted(self, project: str, release: str) -> bool: ...
    def mark_attempted(self, project: str, release: str) -> None: ...
    def forget_attempt(self, project: str, release: str) -> None: ...


class MemoryStore:
    """Process-wide and thread-safe; see the module docstring for its limits."""

    persistent = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], Record] = {}
        self._attempts: set[tuple[str, str]] = set()

    def get(self, project: str, key: str) -> Record:
        with self._lock:
            return copy.deepcopy(self._records.get((project, key), Record()))

    def put_ai(self, project: str, key: str, ai: dict, model: str,
               input_hash: str) -> None:
        with self._lock:
            rec = self._records.setdefault((project, key), Record())
            if rec.ai is not None:
                rec.past_ai.append({"ai": rec.ai, "model": rec.ai_model, "at": rec.ai_at})
            rec.ai, rec.ai_model, rec.ai_at, rec.ai_input = ai, model, now(), input_hash

    def review(self, project: str, key: str, reviewer: str, status: str,
               final: dict, comment: str = "") -> None:
        with self._lock:
            rec = self._records.setdefault((project, key), Record())
            rec.events.append(Event(now(), reviewer, status,
                                    {k: final.get(k, "") for k in FINAL_FIELDS},
                                    rec.final, comment))

    def attempted(self, project: str, release: str) -> bool:
        with self._lock:
            return (project, release) in self._attempts

    def mark_attempted(self, project: str, release: str) -> None:
        with self._lock:
            self._attempts.add((project, release))

    def forget_attempt(self, project: str, release: str) -> None:
        with self._lock:
            self._attempts.discard((project, release))


@st.cache_resource(show_spinner=False)
def store() -> MemoryStore:
    return MemoryStore()
