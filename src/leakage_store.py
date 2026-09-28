"""Where the AI's proposals and the state of each analysis are kept.

Decided with Matteo on 2026-09-28: no permanent archive for now.  This store
lives in the app process: shared by every visitor, kept across reruns and
days, and LOST when the app restarts or redeploys.  An AI proposal is
therefore made again after a restart.

What it keeps, per Jira incident, is the AI proposal; a re-analysis
replaces it and keeps the one it replaced.  (The Key QA review that could
change a proposal was removed on 2026-09-28, at Matteo's request.)

It also keeps the state of each release's AI analysis (running, done,
failed, with progress and errors), so every visitor sees the same one and a
second visitor never starts it twice.

`Store` is the whole interface.  A permanent backend (a private repository,
a database, Jira fields) implements the same methods and replaces
`MemoryStore` in `store()` — nothing else changes.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import streamlit as st

# The values the AI proposes per incident, as the table and the export show them.
FINAL_FIELDS = ("uat_detectability", "category", "testrail_case")


@dataclass
class Run:
    """One AI analysis of a release."""
    state: str                      # "running" / "done" / "failed"
    total: int
    started: float
    done: int = 0
    finished: float = 0.0
    models: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


RUNNING, DONE, FAILED = "running", "done", "failed"


@dataclass
class Record:
    ai: dict | None = None                       # the current AI proposal
    ai_model: str = ""
    ai_at: str = ""
    ai_input: str = ""                           # hash of what the AI read
    past_ai: list[dict] = field(default_factory=list)

    @property
    def final(self) -> dict:
        """The AI's proposal, in the fields the tab counts on."""
        ai = self.ai or {}
        return {k: ai.get(k, "") for k in FINAL_FIELDS}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class Store(Protocol):
    def get(self, project: str, key: str) -> Record: ...
    def put_ai(self, project: str, key: str, ai: dict, model: str, input_hash: str) -> None: ...
    def run(self, project: str, release: str) -> Run | None: ...
    def start_run(self, project: str, release: str, total: int) -> bool: ...
    def run_progress(self, project: str, release: str, done: int, model: str) -> None: ...
    def finish_run(self, project: str, release: str, errors: list[str]) -> None: ...


class MemoryStore:
    """Process-wide and thread-safe; see the module docstring for its limits."""

    persistent = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], Record] = {}
        self._runs: dict[tuple[str, str], Run] = {}

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

    def run(self, project: str, release: str) -> Run | None:
        with self._lock:
            run = self._runs.get((project, release))
            return copy.deepcopy(run) if run else None

    def start_run(self, project: str, release: str, total: int) -> bool:
        """Claim the release's analysis; False if one is already running, so
        two visitors opening the same release never pay for it twice."""
        with self._lock:
            current = self._runs.get((project, release))
            if current is not None and current.state == RUNNING:
                return False
            self._runs[(project, release)] = Run(RUNNING, total, time.time())
            return True

    def run_progress(self, project: str, release: str, done: int, model: str) -> None:
        with self._lock:
            run = self._runs.get((project, release))
            if run is not None:
                run.done += done
                if model and model not in run.models:
                    run.models.append(model)

    def finish_run(self, project: str, release: str, errors: list[str]) -> None:
        with self._lock:
            run = self._runs.get((project, release))
            if run is not None:
                run.errors = list(errors)
                run.finished = time.time()
                # Done only when every incident got its proposal.  A run that
                # ends short without saying why is a failure too: counted as
                # done, the tab would start it again on every visit.
                if run.done >= run.total:
                    run.state = DONE
                else:
                    run.state = FAILED
                    if not run.errors:
                        short = run.total - run.done
                        run.errors = [f"{short} of {run.total} incidents came back without a verdict."]


@st.cache_resource(show_spinner=False)
def store() -> MemoryStore:
    return MemoryStore()
