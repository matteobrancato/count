"""Reload the dashboard's own modules after a deploy — app.py calls this first.

Streamlit re-reads app.py on every run but keeps the modules it has imported.
When Streamlit Cloud pulls a commit while no session is watching the files,
the new app.py runs against the OLD src/ modules.  On 2026-09-28 that first
showed stale text, then crashed the whole app (app.py asked backlog_tab for a
function the old module did not have) until someone rebooted it.

So on every run, before app.py imports anything from src/: if a src module's
source file is newer than the bytecode it was loaded from — or than it was the
last time this guard looked, for a host that writes no bytecode — every src
module is dropped from sys.modules, and the imports that follow load the
deployed code.
Caches survive — Streamlit keys them on each function's code, as when it
reloads a module it watches itself.  Module-level state (the TestRail account
pool, the pacer) starts afresh, as after a reboot.

app.py calls `reload_after_deploy()` on every run, before its own imports from
src/.  This module stays outside src/ so that it is never among the modules it
drops.
"""
from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping

PACKAGE = "src"
# {module: source mtime} as first seen.  Kept on `sys`, which is never
# unloaded, because this module re-runs from scratch on every run.
_SEEN_ATTR = "_count_deploy_guard_seen"


def _ours(name: str) -> bool:
    return name == PACKAGE or name.startswith(PACKAGE + ".")


def stale_modules(modules: Mapping | None = None,
                  seen: dict[str, float] | None = None) -> list[str]:
    """Names of loaded src modules whose source changed after they were
    loaded: since `seen` first recorded it, or — for a module the guard has
    never seen, such as one loaded before the guard existed — newer than its
    bytecode.  The bytecode is trusted only at first sight: on a host that
    cannot rewrite it, it would otherwise look stale on every run."""
    modules = sys.modules if modules is None else modules
    stale = []
    for name, mod in list(modules.items()):
        source = getattr(mod, "__file__", None)
        if not _ours(name) or not source:
            continue
        try:
            changed = os.path.getmtime(source)
        except OSError:
            continue
        compiled = getattr(mod, "__cached__", None)
        try:
            newer_than_bytecode = bool(compiled) and changed > os.path.getmtime(compiled)
        except OSError:
            newer_than_bytecode = False
        known = seen is not None and name in seen
        first_seen = seen.setdefault(name, changed) if seen is not None else changed
        if changed > first_seen or (newer_than_bytecode and not known):
            stale.append(name)
    return stale


def reload_after_deploy() -> list[str]:
    """Drop every src module if any is stale; returns the stale ones."""
    seen = sys.__dict__.setdefault(_SEEN_ATTR, {})
    stale = stale_modules(seen=seen)
    if stale:
        for name in [n for n in sys.modules if _ours(n)]:
            source = getattr(sys.modules[name], "__file__", None)
            try:
                # What is reloaded now is current: remember it as seen, so only
                # a later change of the file can trigger another reload.
                if source:
                    seen[name] = os.path.getmtime(source)
            except OSError:
                seen.pop(name, None)
            del sys.modules[name]
        logging.getLogger(__name__).warning(
            "Deploy detected (%s changed): reloading every src module", ", ".join(stale[:5]))
    return stale
