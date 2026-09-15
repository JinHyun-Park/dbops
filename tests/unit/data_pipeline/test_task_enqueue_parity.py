"""task_enqueue.py is four verbatim copies; they must not drift.

There is no Lambda layer: each data-pipeline directory and each api/ route group is its
own CDK asset, so cross-package sharing in this project is done by duplicating the file
and guarding it with a parity test. alert_evaluator, proactive_monitor and
event_processor each need to enqueue an auto-RCA, and so does api/scenarios (the demo
scenario runner), which lives under api/ and therefore cannot import from data-pipeline
any more than it can import mcp_servers.

The drift this guards is not hypothetical for this codebase: engine_family.py is four
copies with the same kind of test, and the schema-diff contract was duplicated per
consumer until a defect survived six passes because every pass fixed only the copies it
owned.
"""

import hashlib
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
# owner -> the file it must ship. Not a single root any more: the fourth copy lives
# under api/, and hardcoding "data-pipeline/<owner>" is what would have let that one
# drift unnoticed.
_COPIES = {
    "alert_evaluator": _REPO / "data-pipeline" / "alert_evaluator" / "task_enqueue.py",
    "proactive_monitor": _REPO / "data-pipeline" / "proactive_monitor" / "task_enqueue.py",
    "event_processor": _REPO / "data-pipeline" / "event_processor" / "task_enqueue.py",
    "api/scenarios": _REPO / "api" / "scenarios" / "task_enqueue.py",
}
_OWNERS = tuple(_COPIES)


def test_every_owner_has_the_file():
    missing = [o for o, p in _COPIES.items() if not p.is_file()]
    assert missing == [], (
        f"{missing} import task_enqueue but do not ship it. A Lambda can only import "
        "from its OWN asset directory, so a missing copy is an ImportError at runtime, "
        "caught by the caller's except and reported as a skipped enqueue."
    )


def test_the_copies_are_byte_identical():
    digests = {o: hashlib.sha256(p.read_bytes()).hexdigest() for o, p in _COPIES.items()}
    unique = set(digests.values())
    assert len(unique) == 1, (
        "task_enqueue.py copies have diverged:\n"
        + "\n".join(f"  {o}: {d[:16]}" for o, d in digests.items())
        + "\nEdit all four together."
    )


@pytest.mark.parametrize("owner", _OWNERS)
def test_each_copy_exposes_the_parameters_its_callers_pass(owner):
    """A signature check, because the callers pass keyword arguments.

    proactive_monitor and event_processor pass `trigger`; alert_evaluator also passes
    `observed_at`; api/scenarios also passes `dedupe`. A copy that predates any of them
    raises TypeError inside the caller's try/except and silently degrades to no RCA,
    which is exactly the failure mode this whole feature already had for 16 days.
    """
    import ast

    src = _COPIES[owner].read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "enqueue_auto_rca"
    )
    names = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    for required in ("cluster_id", "rule_id", "title", "trigger", "observed_at", "dedupe"):
        assert required in names, f"{owner} copy is missing `{required}`: {sorted(names)}"


def test_dedupe_defaults_to_on():
    """The bypass must be opt-in.

    `dedupe=False` removes the only thing bounding how many auto-RCAs a flapping alert
    can enqueue. It exists for the scenario runner, which has its own stricter guard.
    A copy that flipped the default would leave every automatic producer unbounded, and
    nothing else in this suite would notice.
    """
    import ast

    tree = ast.parse(_COPIES["alert_evaluator"].read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "enqueue_auto_rca"
    )
    idx = [a.arg for a in fn.args.kwonlyargs].index("dedupe")
    default = fn.args.kw_defaults[idx]
    assert isinstance(default, ast.Constant) and default.value is True, (
        "enqueue_auto_rca(dedupe=...) must default to True"
    )
