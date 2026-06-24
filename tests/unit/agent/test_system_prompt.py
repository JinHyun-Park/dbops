"""Tests for build_system_prompt(extra_context, visible_clusters) — Task 3 tenancy.

Loaded via importlib to avoid importing the agent package directly (which would
create __pycache__ under agent/ and cause AgentCore Runtime deploy failures).
teardown_module removes any __pycache__ directories created under agent/.
"""
import importlib.util
import shutil
import sys
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[3] / "agent"


def _load():
    p = _AGENT / "prompts/system_prompt.py"
    agent_str = str(_AGENT)
    _added = agent_str not in sys.path
    if _added:
        sys.path.insert(0, agent_str)
    spec = importlib.util.spec_from_file_location("agent_prompts_system_prompt", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def teardown_module(_):
    for pc in _AGENT.rglob("__pycache__"):
        if "_deps" not in str(pc):
            shutil.rmtree(pc, ignore_errors=True)


def test_visible_clusters_constraint_block_present():
    sp = _load()
    out = sp.build_system_prompt("", visible_clusters={"c-open", "c-teamA"})
    assert "접근 제한" in out
    assert "c-open" in out
    assert "c-teamA" in out


def test_visible_clusters_none_no_constraint_block():
    sp = _load()
    base = sp.build_system_prompt("")
    out = sp.build_system_prompt("", visible_clusters=None)
    # No constraint block when visible_clusters is None
    assert "접근 제한" not in out
    # Output should be identical to the no-arg call
    assert out == base


def test_visible_clusters_empty_set():
    sp = _load()
    out = sp.build_system_prompt("", visible_clusters=set())
    assert "접근 제한" in out
    assert "(없음)" in out


def test_existing_extra_context_preserved_with_visible_clusters():
    sp = _load()
    out = sp.build_system_prompt("ORGCHART: alice owns prod-1", visible_clusters={"c-open"})
    assert "ORGCHART: alice owns prod-1" in out
    assert "접근 제한" in out
    assert "c-open" in out
