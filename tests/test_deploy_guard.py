"""The deploy guard: after a deploy, app.py must not run against old modules.

On 2026-09-28 Streamlit Cloud ran the new app.py with the src/ modules it had
imported before the pull, and the whole app crashed on a function the old
backlog_tab did not have.  What is locked here is the detection only — the
sys.modules purge itself is what Streamlit's own watcher does.
"""
from __future__ import annotations

import os
import types

import deploy_guard as dg


def _module(tmp_path, name, source_age, bytecode_age):
    src = tmp_path / f"{name}.py"
    src.write_text("x = 1\n", encoding="utf-8")
    pyc = tmp_path / f"{name}.pyc"
    pyc.write_bytes(b"")
    now = 1_800_000_000
    os.utime(src, (now - source_age, now - source_age))
    os.utime(pyc, (now - bytecode_age, now - bytecode_age))
    mod = types.ModuleType(name)
    mod.__file__, mod.__cached__ = str(src), str(pyc)
    return mod


def test_a_source_newer_than_its_bytecode_is_stale(tmp_path):
    modules = {"src.ui.backlog_tab": _module(tmp_path, "a", source_age=10, bytecode_age=100),
               "src.leakage": _module(tmp_path, "b", source_age=100, bytecode_age=10)}
    assert dg.stale_modules(modules) == ["src.ui.backlog_tab"]


def test_other_packages_are_never_touched(tmp_path):
    modules = {"streamlit.x": _module(tmp_path, "c", source_age=10, bytecode_age=100)}
    assert dg.stale_modules(modules) == []


def test_without_bytecode_a_change_since_first_seen_is_stale(tmp_path):
    """A host that writes no bytecode: the guard compares with the source's
    date the first time it saw the module."""
    mod = _module(tmp_path, "d", source_age=100, bytecode_age=100)
    os.remove(mod.__cached__)
    seen: dict[str, float] = {}
    assert dg.stale_modules({"src.x": mod}, seen) == []           # first sight
    later = os.path.getmtime(mod.__file__) + 60
    os.utime(mod.__file__, (later, later))                         # a deploy
    assert dg.stale_modules({"src.x": mod}, seen) == ["src.x"]


def test_app_runs_the_guard_before_anything_from_src():
    import pathlib
    lines = pathlib.Path("app.py").read_text(encoding="utf-8").splitlines()
    guard = next(i for i, ln in enumerate(lines)
                 if ln.startswith("deploy_guard.reload_after_deploy()"))
    first_src = next(i for i, ln in enumerate(lines) if ln.startswith("from src"))
    assert guard < first_src


def test_stale_bytecode_is_trusted_only_at_first_sight(tmp_path):
    """A host that cannot rewrite bytecode keeps an old .pyc: after one reload
    the module must not look stale again on every run."""
    mod = _module(tmp_path, "e", source_age=10, bytecode_age=100)
    seen: dict[str, float] = {}
    assert dg.stale_modules({"src.y": mod}, seen) == ["src.y"]    # first sight
    assert dg.stale_modules({"src.y": mod}, seen) == []           # known now


# ── computed caches: cleared on a deploy, downloads kept (2026-10-13) ────────
def test_a_deploy_asks_the_outgoing_code_to_drop_its_computed_numbers():
    calls = []
    fresh = types.SimpleNamespace(clear_computed=lambda: calls.append(1) or 17)
    assert dg.clear_computed({"src.freshness": fresh}) == 17
    assert calls == [1]


def test_old_code_without_the_hook_or_a_failing_hook_never_blocks_a_deploy():
    assert dg.clear_computed({}) == 0
    assert dg.clear_computed({"src.freshness": types.SimpleNamespace()}) == 0

    def boom():
        raise RuntimeError("cache backend down")
    assert dg.clear_computed({"src.freshness": types.SimpleNamespace(clear_computed=boom)}) == 0


def test_every_cache_is_either_downloaded_data_or_a_computed_number():
    """A cache added under plain @st.cache_data is downloaded data and survives
    a deploy; anything computed must use `computed_cache`, or new logic would
    be served old numbers until the next full reload."""
    import pathlib
    import re
    downloaded = {"_fetch_cases_cached", "_fetch_labels_cached", "_fetch_sections_cached",
                  "fetch_case_fields", "fetch_case_types", "fetch_priorities", "fetch_suite",
                  "field_ids_by_name", "project_versions", "uat_issues", "incidents",
                  "_jira_issues", "stamp"}
    found = set()
    for path in pathlib.Path("src").rglob("*.py"):
        found |= set(re.findall(r"@st\.cache_data\([^\n]*\)\ndef (\w+)",
                                path.read_text(encoding="utf-8")))
    assert found == downloaded


def test_clear_computed_clears_what_was_registered(monkeypatch):
    from src import freshness
    cleared = []
    fake = types.SimpleNamespace(clear=lambda: cleared.append(1))
    monkeypatch.setattr(freshness, "_COMPUTED", [fake, fake])
    assert freshness.clear_computed() == 2 and cleared == [1, 1]
