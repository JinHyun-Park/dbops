"""The Anomalies panel must not render "이상 징후 없음" when nothing was checked.

`GET /api/dashboard/{id}/anomalies` returns `baseline_mode` (seasonal / flat /
none / no_samples, same derivation as the detect_anomalies MCP tool). The panel
has to branch on it:

  * `none`       there is no baseline of either kind, so zero anomalies means
                 "nothing was scored". Samples ARE arriving: wait for history.
  * `no_samples` there are no recent cluster-level samples at all, so nothing
                 COULD be scored. Waiting does not help: check collection.

Both are reachable right after deploy for every family, and newly reachable for
documentdb / dynamodb / elasticache which only just got the panel at all.

No JS runtime in CI, so instead of asserting strings exist, the EmptyState
branch chain is PARSED and MODELLED (same idea as `_apply_dim_filter` in
test_metric_filters.py): the guards are read out of the source, each is
translated into a Python predicate, and every case below asserts which branch a
given `meta` actually reaches. An unrecognized guard fails loudly, so a fourth
branch has to be recorded here deliberately.

What this does NOT pin is branch ORDER, and no comment should imply otherwise.
Mutation-checked both ways: swapping the `no_samples` and `none` guards fails 0
tests (they are mutually exclusive equality tests on the same field, so the swap
is a semantic no-op), and moving the `meta.failed` guard below both of them also
fails 0 tests. The second one is order-sensitive in principle but unreachable in
practice: the panel's `.catch` does `setMeta({ failed: true })`, replacing the
whole object, so `failed` never arrives alongside a mode.
"""

import re
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
    return _PANEL[start:end]


# Each early-return guard EmptyState may carry, as a predicate over `meta`.
# Parsing refuses anything not listed, so adding a branch means recording it
# here (and saying, in a test, which meta reaches it).
_PREDICATES = {
    "meta.failed": lambda m: bool(m.get("failed")),
    'meta.mode === "no_samples"': lambda m: m.get("mode") == "no_samples",
    'meta.mode === "none"': lambda m: m.get("mode") == "none",
}


def _branches():
    """[(guard_source, predicate, branch_jsx)] as they appear in the source, plus
    the fallthrough as the last entry with a guard of None. Source position is how
    an if-chain is modelled, not a property under test: see the module docstring
    for why no ordering here is load-bearing."""
    body = _empty_state_body()
    guards = list(re.finditer(r"^  if \((.*?)\) \{", body, re.M))
    assert guards, "EmptyState has no branches at all"
    out = []
    for g in guards:
        cond = g.group(1)
        assert cond in _PREDICATES, (
            f"unrecognized EmptyState guard {cond!r}. Add it to _PREDICATES and "
            "assert which meta reaches it: an unmodelled branch could be "
            "swallowing a state the panel is supposed to distinguish."
        )
        close = body.index("\n  }", g.end())
        out.append((cond, _PREDICATES[cond], body[g.start():close]))
    tail_from = body.index("\n  }", guards[-1].end())
    out.append((None, lambda _m: True, body[tail_from:]))
    return out


def _rendered(meta: dict) -> str:
    """The JSX EmptyState actually reaches for this `meta`."""
    for _cond, pred, jsx in _branches():
        if pred(meta):
            return _flat(jsx)
    raise AssertionError("no branch matched, EmptyState has no fallthrough return")


def test_api_still_returns_the_signal_the_panel_branches_on():
    """A frontend-only guard would pass forever if the field vanished server-side."""
    assert '"baseline_mode":' in _HANDLER
    assert '"total_checked":' in _HANDLER
    assert '"no_samples"' in _HANDLER, "the api no longer distinguishes the no-data state"


def test_panel_reads_baseline_mode_from_the_response():
    flat = _flat(_PANEL)
    assert "d.baseline_mode" in flat, (
        "the panel ignores baseline_mode, so 'no baseline trained' and 'no "
        "anomalies found' render identically"
    )


def test_no_baseline_trained_does_not_claim_there_are_no_anomalies():
    jsx = _rendered({"mode": "none"})
    assert _ALL_CLEAR not in jsx, (
        "the untrained-baseline state must not be phrased as a clean bill of health"
    )
    assert "baseline" in jsx          # says what is missing
    assert "2주" in jsx               # ...and what to wait for
    assert "ETL" not in jsx, (
        "this state now KNOWS samples are arriving (the probe found them), so "
        "hedging with 'check your ETL' is advice the data does not support: it "
        "belongs to the no_samples branch"
    )


def test_no_recent_samples_sends_the_operator_to_collection_not_to_wait():
    """DEFECT 1: this used to render as the cold-start message, so a dead
    collector was reported as "wait about two weeks"."""
    jsx = _rendered({"mode": "no_samples"})
    assert _ALL_CLEAR not in jsx
    assert "ETL" in jsx
    assert "2주" not in jsx, "waiting does not fix a missing-samples state"
    assert jsx != _rendered({"mode": "none"}), (
        "no_samples and none render identically, which is the collapse this fixes"
    )


def test_a_failed_lookup_is_not_rendered_as_no_anomalies_either():
    jsx = _rendered({"failed": True})
    assert _ALL_CLEAR not in jsx
    # A mode alongside `failed` is not a state the panel can actually produce (the
    # `.catch` replaces meta wholesale), so this pins the guard's independence from
    # leftover fields, NOT a "failure wins over a stale mode" behaviour.
    assert _rendered({"failed": True, "mode": "seasonal"}) == jsx


def test_the_all_clear_copy_is_reached_only_by_a_scored_cluster():
    for mode in ("seasonal", "flat"):
        assert _ALL_CLEAR in _rendered({"mode": mode, "checked": 12}), mode


def test_deploy_skew_undefined_mode_falls_through_to_the_all_clear():
    """DEFECT 3, pinned rather than changed. An api Lambda deployed before
    baseline_mode existed sends no mode at all. Degrading to the old unqualified
    copy for the few minutes of a rolling deploy is DELIBERATE: a fourth alarming
    state for a field the old API never sent would alarm every operator on every
    deploy. This test exists so the next reader can tell the choice from an
    oversight, and fails if undefined starts hitting an alarming branch."""
    jsx = _rendered({})
    assert _ALL_CLEAR in jsx
    assert jsx == _rendered({"mode": "seasonal"})
    assert "// Deploy skew" in _empty_state_body(), (
        "the intent comment is the other half of this pin: keep them together"
    )


def test_flat_baseline_rows_keep_their_lower_confidence_qualifier():
    """The per-row `flat` badge stays; the empty state gains the same caveat, so
    "quiet, but judged against a 7-day flat baseline" is not read as certainty."""
    assert 'a.mode === "flat"' in _flat(_PANEL), "per-row flat badge lost"
    # The caveat is a note inside the all-clear branch, not a branch of its own,
    # so this one is guard-shaped rather than result-shaped: `flat` must still
    # earn the lower-confidence line the other modes do not get.
    all_clear = _rendered({"mode": "seasonal", "checked": 12})
    assert 'meta.mode === "flat" ? "이 시간대의 seasonal baseline이' in all_clear
    assert "신뢰도 낮음" in all_clear
