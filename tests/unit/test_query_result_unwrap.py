"""Repo-wide guard: `cache.execute(...)` returns a QueryResult, not a list.

WHAT WENT WRONG WITHOUT THIS
----------------------------
`CacheClient.execute` returns a `QueryResult` object. Code that then writes

    rows = cache.execute(sql, params)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        ...

is ALWAYS in the else branch, because a QueryResult is not a list. The unwrap
`rows = getattr(rows, "rows", rows)` is what makes it work, and
`operations/handler.py` has it while `simulation/handler.py` did not.

Measured live on 2026-08-02 by invoking all 64 gateway tools against 9 real
clusters. That one missing line broke every simulation tool, in both directions
at once:

  * `_resolve_family` always returned None, and the `simulation` capability gate
    is DEFAULT-PERMIT on a None family, so the six Aurora-only tools ran on every
    engine. `estimate_upgrade_impact` told the operator a Valkey cache would be
    down "~23분 (pg_upgrade 동안 writer 중단)". A DBA can plan a maintenance
    window from a number like that.
  * the three POSITIVE gates refuse a None family, so
    simulate_dynamodb_capacity_cost, simulate_elasticache_node_resize and
    simulate_rds_instance_rightsizing returned unsupported_engine on 9 of 9
    clusters INCLUDING their own family. Three shipped features, completely dark.
  * `rds_rightsizing` had the same omission twice more, so even with the gate
    fixed it would have reported "cluster_meta를 찾지 못했습니다" and
    "not enough samples" regardless of what was collected.

Unit tests could not catch any of it: they hand the impl a MagicMock or a plain
list, so the isinstance check behaves differently than it does in production.
That is exactly why this guard reads the SOURCE instead of calling the code.
"""

import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_ROOTS = ("mcp-servers/mcp_servers", "api", "data-pipeline")

_ASSIGN = re.compile(
    r"^\s*(\w+)\s*=\s*(?:cache|self\.cache|_cache)\.execute(?:_on_target)?\(")
_WINDOW = 30


def _unwrapped(var, window):
    """Any of these proves the author knew it was a QueryResult."""
    return (
        f'getattr({var}, "rows"' in window
        or f"getattr({var}, 'rows'" in window
        or f"{var}.rows" in window
        or f'{var}["rows"]' in window
        or f"{var}.row_count" in window
        or f"{var}.columns" in window
    )


def _violations():
    out = []
    for root in _ROOTS:
        base = _REPO / root
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            try:
                lines = p.read_text().split("\n")
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(lines):
                m = _ASSIGN.match(line)
                if not m:
                    continue
                var = m.group(1)
                window = "\n".join(lines[i + 1: i + 1 + _WINDOW])
                # Only a list-shaped assumption is a bug. Passing the QueryResult
                # onward, or reading .rows later, is fine.
                if re.search(rf"isinstance\(\s*{var}\s*,\s*list\s*\)", window) and not _unwrapped(var, window):
                    out.append((p.relative_to(_REPO), i + 1, var, line.strip()))
    return out


# --------------------------------------------------------------------------
# Controls. A census that silently matches nothing is worse than no census.
# --------------------------------------------------------------------------

_BAD = '''
def f(cache, cid):
    rows = cache.execute("SELECT 1", {"cid": cid})
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
'''

_GOOD_UNWRAPPED = '''
def f(cache, cid):
    rows = cache.execute("SELECT 1", {"cid": cid})
    rows = getattr(rows, "rows", rows)
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return rows[0]
'''

_GOOD_NO_LIST_ASSUMPTION = '''
def f(cache, cid):
    result = cache.execute("SELECT 1", {"cid": cid})
    return result.rows[0] if result.rows else {}
'''


def _scan_text(text):
    lines = text.split("\n")
    hits = []
    for i, line in enumerate(lines):
        m = _ASSIGN.match(line)
        if not m:
            continue
        var = m.group(1)
        window = "\n".join(lines[i + 1: i + 1 + _WINDOW])
        if re.search(rf"isinstance\(\s*{var}\s*,\s*list\s*\)", window) and not _unwrapped(var, window):
            hits.append(i + 1)
    return hits


def test_detector_flags_the_real_shape_of_the_bug():
    assert _scan_text(_BAD), "detector missed the exact pattern that shipped"


@pytest.mark.parametrize("ok", [_GOOD_UNWRAPPED, _GOOD_NO_LIST_ASSUMPTION])
def test_detector_accepts_correct_code(ok):
    assert not _scan_text(ok)


def test_census_scans_the_whole_tree():
    n = sum(
        1
        for root in _ROOTS
        if (_REPO / root).is_dir()
        for p in (_REPO / root).rglob("*.py")
        if "__pycache__" not in p.parts
    )
    assert n > 200, f"expected the whole tree, scanned only {n} files"


def test_no_queryresult_treated_as_a_list():
    v = _violations()
    assert not v, (
        "cache.execute() result assumed to be a list without a .rows unwrap "
        "(add `x = getattr(x, 'rows', x)`):\n"
        + "\n".join(f"  {f}:{ln}  var={var}\n      {line}" for f, ln, var, line in v)
    )
