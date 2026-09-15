"""task_enqueue.py is three verbatim copies; they must not drift.

There is no Lambda layer: each data-pipeline directory is its own CDK asset, so
cross-package sharing in this project is done by duplicating the file and guarding it
with a parity test. alert_evaluator, proactive_monitor and event_processor each need to
enqueue an auto-RCA, so each carries a copy.

The drift this guards is not hypothetical for this codebase: engine_family.py is four
copies with the same kind of test, and the schema-diff contract was duplicated per
consumer until a defect survived six passes because every pass fixed only the copies it
owned.
"""

import hashlib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3] / "data-pipeline"
_OWNERS = ("alert_evaluator", "proactive_monitor", "event_processor")


def _copies():
    return {owner: _ROOT / owner / "task_enqueue.py" for owner in _OWNERS}


def test_every_owner_has_the_file():
    missing = [o for o, p in _copies().items() if not p.is_file()]
    assert missing == [], (
        f"{missing} import task_enqueue but do not ship it. A data-pipeline Lambda can "
        "only import from its OWN directory, so a missing copy is an ImportError at "
        "runtime, caught by the caller's except and reported as a skipped enqueue."
    )


def test_the_copies_are_byte_identical():
    digests = {o: hashlib.sha256(p.read_bytes()).hexdigest() for o, p in _copies().items()}
    unique = set(digests.values())
    assert len(unique) == 1, (
        "task_enqueue.py copies have diverged:\n"
        + "\n".join(f"  {o}: {d[:16]}" for o, d in digests.items())
        + "\nEdit all three together."
    )


@pytest.mark.parametrize("owner", _OWNERS)
def test_each_copy_exposes_the_parameters_its_callers_pass(owner):
    """A signature check, because the callers pass keyword arguments.

    proactive_monitor and event_processor pass `trigger`; alert_evaluator also passes
    `observed_at`. A copy that predates either parameter raises TypeError inside the
    caller's try/except and silently degrades to no RCA, which is exactly the failure
    mode this whole feature already had for 16 days.
    """
    import ast

    src = (_ROOT / owner / "task_enqueue.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "enqueue_auto_rca"
    )
    names = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    for required in ("cluster_id", "rule_id", "title", "trigger", "observed_at"):
        assert required in names, f"{owner} copy is missing `{required}`: {sorted(names)}"
