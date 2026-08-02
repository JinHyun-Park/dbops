"""Repo-wide guard: a time column compared to a bound parameter must be CAST.

WHY THIS IS A TEST AND NOT A CODE REVIEW NOTE
---------------------------------------------
The RDS Data API sends every bound parameter as `stringValue`, so PostgreSQL
receives `text`. There is no `timestamp with time zone >= text` operator, so a
statement like

    WHERE ts >= :start_time

fails with SQLState 42883 before it touches a row. `::timestamptz` on the
parameter is load-bearing.

This is a test because a unit test with a MagicMock cache CANNOT catch it: the
fake accepts any SQL string and returns canned rows, so the tool looks correct
while the database rejects it. `compare_periods` shipped exactly that way, dead
for every engine family, with a green test asserting `execute.call_count == 2`.
A live probe of all 64 tools found it, not the suite.

The rule is repo-wide because the fix is per-site: 45 other comparison sites had
the cast and one did not, so nothing but a census keeps the odd one out visible.
"""

import pathlib
import re

import pytest

# Time-typed columns in the cache schema (data-pipeline/sql/schema*.sql).
_TIME_COLS = (
    "ts",
    "snapshot_time",
    "event_time",
    "collected_at",
    "window_start",
    "window_end",
    "opened_at",
    "resolved_at",
    "last_seen",
    "first_seen",
)

# A time column compared to :param where the param is NOT followed by a cast.
#
# The negative lookahead is `(?![\w:])`, and the exact form matters. A first
# version used `(?!\s*::)`, which SILENTLY MATCHED EVERYTHING: `\w+` backtracks,
# so `:start_time::timestamptz` matched with the param captured as `start_tim`
# and the lookahead inspecting `e`, not `::`. That regex reported 46 violations
# where there were 2. Requiring "no word char AND no colon" after the name
# removes the backtracking escape.
_UNCAST = re.compile(
    r"\b(" + "|".join(_TIME_COLS) + r")\s*(?:>=|<=|>|<|=)\s*:(\w+)(?![\w:])",
    re.IGNORECASE,
)

# The literal-column pattern above has a blind spot: a predicate whose column name
# is INTERPOLATED. `cache_client._build_query` builds
# `f"{time_column} >= :start_time"`, so no literal column name appears and the
# census walked straight past the one shared query builder in the repo, while
# flagging nothing. That single site broke the start_time/end_time arguments of
# get_top_queries, get_slow_queries and get_pi_metrics.
#
# So also flag any `{...} <op> :param` with no cast. Restricted to parameters whose
# NAME looks temporal, because an interpolated column compared to, say, :cluster_id
# is ordinary and must not be reported.
_TIME_PARAMS = r"(?:start_time|end_time|since|until|from_ts|to_ts|anchor|change_point|ts|at|before|after)"
_UNCAST_INTERPOLATED = re.compile(
    r"\{\s*\w+\s*\}\s*(?:>=|<=|>|<|=)\s*:(" + _TIME_PARAMS + r")(?![\w:])",
    re.IGNORECASE,
)

# DynamoDB expressions use the same `:name` placeholder syntax but are not SQL
# and have no cast concept.
_NOT_SQL = ("Expression", "KeyCondition", "UpdateExpression", "ConditionExpression")

_ROOTS = ("mcp-servers/mcp_servers", "api", "data-pipeline")
_REPO = pathlib.Path(__file__).resolve().parents[2]


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
                text = p.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.split("\n"), 1):
                if any(k in line for k in _NOT_SQL):
                    continue
                for m in _UNCAST.finditer(line):
                    out.append((p.relative_to(_REPO), lineno, m.group(1), m.group(2),
                                line.strip()))
                for m in _UNCAST_INTERPOLATED.finditer(line):
                    out.append((p.relative_to(_REPO), lineno, "<interpolated>",
                                m.group(1), line.strip()))
    return out


# --------------------------------------------------------------------------
# Controls first. A census test that matches nothing passes forever and proves
# nothing, which is precisely how the bad regex above would have shipped.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "WHERE ts >= :start_time AND ts < :end_time",
    "AND snapshot_time > :since",
    "  AND event_time <= :until ",
    "WHERE ts = :exact",
])
def test_regex_catches_an_uncast_parameter(bad):
    assert _UNCAST.search(bad), f"regex failed to flag a genuine violation: {bad}"


def test_interpolated_predicate_control_catches_the_builder_shape():
    """The exact line cache_client._build_query used to emit."""
    bad = 'conditions.append(f"{time_column} >= :start_time")'
    assert _UNCAST_INTERPOLATED.search(bad)
    good = 'conditions.append(f"{time_column} >= :start_time::timestamptz")'
    assert not _UNCAST_INTERPOLATED.search(good)
    # an interpolated column compared to a non-temporal param is ordinary
    assert not _UNCAST_INTERPOLATED.search('f"{col} = :cluster_id"')


@pytest.mark.parametrize("good", [
    "WHERE ts >= :start_time::timestamptz AND ts < :end_time::timestamptz",
    "AND snapshot_time >= :change_point::timestamptz - (:hours || ' hours')::interval",
    "AND ts BETWEEN NOW() - INTERVAL '7 days' AND NOW()",
    "AND snapshot_time > NOW() - INTERVAL '24 hours'",
    # a non-time column bound without a cast is fine
    "WHERE cluster_id = :cluster_id AND metric_type = :metric_type",
])
def test_regex_accepts_correct_sql(good):
    assert not _UNCAST.search(good), f"regex false-positived on valid SQL: {good}"


def test_no_uncast_timestamp_comparison_anywhere():
    v = _violations()
    assert not v, "time column compared to an UNCAST bound parameter (add ::timestamptz):\n" + "\n".join(
        f"  {f}:{ln}  {col} vs :{param}\n      {line}" for f, ln, col, param, line in v
    )


def test_the_census_actually_scans_files():
    """Guards the guard: if the roots move or the glob breaks, the violation test
    above passes by scanning nothing."""
    scanned = sum(
        1
        for root in _ROOTS
        if (_REPO / root).is_dir()
        for p in (_REPO / root).rglob("*.py")
        if "__pycache__" not in p.parts
    )
    assert scanned > 200, f"expected to scan the whole tree, only saw {scanned} files"
