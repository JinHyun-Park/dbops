"""The Anomalies panel must not render "이상 징후 없음" when nothing was checked.

`GET /api/dashboard/{id}/anomalies` now returns `baseline_mode`
(seasonal / flat / none, same derivation as the detect_anomalies MCP tool). The
panel has to branch on it: with `none` there is no baseline of either kind, so
zero anomalies means "nothing was scored", not "all clear". Reachable right
after deploy for every family, and newly reachable for documentdb / dynamodb /
elasticache which only just got the panel at all.

Regex-based on purpose, same as test_anomalies_panel_families.py: no JS runtime
in CI, and both sides are flat literals.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PANEL = (
    _ROOT / "frontend/src/components/dashboard/anomalies-panel.tsx"
).read_text()
_HANDLER = (_ROOT / "api/dashboard/handler.py").read_text()

_ALL_CLEAR = "최근 4시간 동안 이상 징후 없음"


def _flat(s: str) -> str:
    return " ".join(s.split())


def _empty_state_body() -> str:
    """The EmptyState component, from its declaration to the next top-level one."""
    start = _PANEL.index("function EmptyState(")
    end = _PANEL.index("\nfunction ", start + 1)
    return _flat(_PANEL[start:end])


def test_api_still_returns_the_signal_the_panel_branches_on():
    """A frontend-only guard would pass forever if the field vanished server-side."""
    assert '"baseline_mode":' in _HANDLER
    assert '"total_checked":' in _HANDLER


def test_panel_reads_baseline_mode_from_the_response():
    flat = _flat(_PANEL)
    assert "d.baseline_mode" in flat, (
        "the panel ignores baseline_mode, so 'no baseline trained' and 'no "
        "anomalies found' render identically"
    )
    assert 'meta.mode === "none"' in flat


def test_the_no_baseline_branch_does_not_claim_there_are_no_anomalies():
    body = _empty_state_body()
    tail = body[body.index('meta.mode === "none"') :]
    # from the branch test up to the NEXT return, i.e. exactly this branch's JSX
    first = tail.index("return (")
    none_branch = tail[: tail.index("return (", first + 1)]
    assert _ALL_CLEAR not in none_branch, (
        "the untrained-baseline state must not be phrased as a clean bill of health"
    )
    # It has to say what to wait for / what to check, not just "unknown".
    assert "baseline" in none_branch
    assert "ETL" in none_branch


def test_a_failed_lookup_is_not_rendered_as_no_anomalies_either():
    body = _empty_state_body()
    assert "meta.failed" in body
    failed_branch = body[body.index("meta.failed") : body.index('meta.mode === "none"')]
    assert _ALL_CLEAR not in failed_branch


def test_the_all_clear_copy_still_exists_for_the_case_that_earns_it():
    assert _ALL_CLEAR in _empty_state_body()


def test_flat_baseline_rows_keep_their_lower_confidence_qualifier():
    """The per-row `flat` badge stays; the empty state gains the same caveat, so
    "quiet, but judged against a 7-day flat baseline" is not read as certainty."""
    flat = _flat(_PANEL)
    assert 'a.mode === "flat"' in flat, "per-row flat badge lost"
    assert 'meta.mode === "flat"' in _empty_state_body()
