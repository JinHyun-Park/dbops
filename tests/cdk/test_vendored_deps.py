"""The synth-time gate that keeps agent/_deps consistent with its requirements file.

These tests use tmp fixtures rather than the real 68MB `agent/_deps`, because that tree
is gitignored and therefore ABSENT in CI. Testing against the real one would make this
file a no-op exactly where it needs to hold.

The regression being guarded is measured, not hypothetical: on 2026-08-03 the shipped
runtime artifact carried pyjwt 2.12.1 and mcp 1.27.1 while agent/requirements.txt
already declared >=2.13.0 and >=1.28.1, for 20 known advisories in the deployed image.
pyjwt verifies the caller's id_token in agent/tenancy.py, so it sits on the tenancy
isolation boundary.
"""

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "cdk"))

from vendored_deps import (  # noqa: E402
    assert_vendored_deps_fresh,
    declared_floors,
    stale_vendored_deps,
    vendored_versions,
)


@pytest.fixture(autouse=True)
def _no_opt_out(monkeypatch):
    """The opt-out is process-global, so clear it for every test in this module: a value
    leaking in from the environment would silently turn the absence tests green."""
    monkeypatch.delenv("DBOPS_SYNTH_WITHOUT_AGENT_DEPS", raising=False)


def _tree(tmp_path, requirements: str, installed: dict | None):
    """Write a requirements file, and a `_deps` dir of `*.dist-info` names.
    `installed=None` means the dir does not exist at all (the CI state)."""
    req = tmp_path / "requirements.txt"
    req.write_text(requirements)
    deps = tmp_path / "_deps"
    if installed is not None:
        deps.mkdir()
        for name, version in installed.items():
            (deps / f"{name}-{version}.dist-info").mkdir()
    return req, deps


def test_the_measured_regression_is_caught(tmp_path):
    """The exact shipped defect: floors raised, tree never rebuilt."""
    req, deps = _tree(
        tmp_path,
        "PyJWT[crypto]>=2.13.0\nmcp>=1.28.1\nboto3>=1.43.38\n",
        {"pyjwt": "2.12.1", "mcp": "1.27.1", "boto3": "1.43.62"},
    )
    problems = stale_vendored_deps(req, deps)

    assert len(problems) == 2, problems
    joined = "\n".join(problems)
    assert "pyjwt" in joined and "2.12.1" in joined
    assert "mcp" in joined and "1.27.1" in joined
    # boto3 satisfied its floor, so it must NOT be reported. Without this the test
    # would pass on a checker that flags everything.
    assert "boto3" not in joined


def test_a_consistent_tree_reports_nothing(tmp_path):
    """Negative control: the state right after build-deps.sh."""
    req, deps = _tree(
        tmp_path,
        "PyJWT[crypto]>=2.13.0\nmcp>=1.28.1\n",
        {"pyjwt": "2.13.0", "mcp": "1.29.0"},
    )
    assert stale_vendored_deps(req, deps) == []


def test_an_absent_tree_IS_a_violation_by_default(tmp_path, monkeypatch):
    """The state of every FRESH CLONE, and the worse of the two failure modes.

    Measured 2026-08-11 on a clean clone of the public repo: with frontend/out stubbed,
    `cdk synth` exited 0 with no `_deps` at all, so a deploy from it would ship an
    AgentCore Runtime with zero vendored dependencies and every chat turn would die at
    import. An earlier version of this gate returned [] here to keep CI green, which
    optimised for CI and silently broke the newcomer path.
    """
    monkeypatch.delenv("DBOPS_SYNTH_WITHOUT_AGENT_DEPS", raising=False)
    req, deps = _tree(tmp_path, "PyJWT[crypto]>=2.13.0\n", None)
    assert not deps.exists()

    problems = stale_vendored_deps(req, deps)
    assert len(problems) == 1, problems
    assert "DOES NOT EXIST" in problems[0]


def test_the_absent_tree_opt_out_is_explicit_and_exact(tmp_path, monkeypatch):
    """CI's two synth paths opt out; nothing else should be able to, by accident.

    Only the exact string "1" opts out, so a stray empty/0/true value still refuses.
    """
    req, deps = _tree(tmp_path, "PyJWT[crypto]>=2.13.0\n", None)

    monkeypatch.setenv("DBOPS_SYNTH_WITHOUT_AGENT_DEPS", "1")
    assert stale_vendored_deps(req, deps) == []

    for sloppy in ("", "0", "true", "yes", "TRUE"):
        monkeypatch.setenv("DBOPS_SYNTH_WITHOUT_AGENT_DEPS", sloppy)
        assert len(stale_vendored_deps(req, deps)) == 1, f"{sloppy!r} must not opt out"


def test_the_raising_wrapper_names_build_deps_for_an_absent_tree(tmp_path, monkeypatch):
    """A fresh-clone user must be told the command, not just that something is wrong."""
    monkeypatch.delenv("DBOPS_SYNTH_WITHOUT_AGENT_DEPS", raising=False)
    req, deps = _tree(tmp_path, "PyJWT[crypto]>=2.13.0\n", None)

    with pytest.raises(RuntimeError) as e:
        assert_vendored_deps_fresh(req, deps)

    msg = str(e.value)
    assert "build-deps.sh" in msg
    assert "deploy.sh" in msg
    assert "DOES NOT EXIST" in msg


def test_a_declared_package_missing_from_the_tree_is_reported(tmp_path):
    """PyJWT was declared explicitly precisely so a rebuild could not drop it
    transitively; absence has to be louder than silence."""
    req, deps = _tree(tmp_path, "PyJWT[crypto]>=2.13.0\nmcp>=1.28.1\n", {"mcp": "1.29.0"})
    problems = stale_vendored_deps(req, deps)
    assert len(problems) == 1
    assert "pyjwt" in problems[0] and "NOT PRESENT" in problems[0]


def test_name_normalization_matches_requirements_to_dist_info(tmp_path):
    """`PyJWT[crypto]` on one side, `pyjwt-2.13.0.dist-info` on the other, and
    `python-dateutil` installs as `python_dateutil-*`. A naive compare misses all
    three and reports a healthy tree as broken."""
    req, deps = _tree(
        tmp_path,
        "PyJWT[crypto]>=2.13.0\npython-dateutil>=2.9.0\nopentelemetry-sdk>=1.41.0\n",
        {"pyjwt": "2.13.0", "python_dateutil": "2.9.0.post0", "opentelemetry_sdk": "1.41.1"},
    )
    assert stale_vendored_deps(req, deps) == []


def test_only_the_ge_operator_is_read(tmp_path):
    """Comments, bare names and `==` pins carry no drift risk and must be ignored
    rather than misparsed into a phantom floor."""
    req, _ = _tree(tmp_path, "# a comment >=99.0.0\nbare-package\npinned==1.0.0\nreal>=2.0.0\n", {})
    assert declared_floors(req) == {"real": "2.0.0"}


def test_an_uncomparable_version_is_reported_not_passed(tmp_path):
    """This gate exists because a silent pass already shipped 20 advisories.

    The version here has no hyphen on purpose: PEP 440 forbids one, and a hyphenated
    value would be split into the NAME by rpartition, testing a case that cannot occur.
    """
    req, deps = _tree(tmp_path, "weird>=1.0.0\n", {"weird": "abc"})
    problems = stale_vendored_deps(req, deps)
    assert len(problems) == 1
    assert "cannot compare" in problems[0], problems


def test_vendored_versions_reads_dist_info_names(tmp_path):
    _, deps = _tree(tmp_path, "", {"pyjwt": "2.13.0", "starlette": "1.3.1"})
    assert vendored_versions(deps) == {"pyjwt": "2.13.0", "starlette": "1.3.1"}


def test_the_raising_wrapper_names_the_fix_command(tmp_path):
    """A gate that blocks a deploy without saying how to unblock it gets bypassed."""
    req, deps = _tree(tmp_path, "PyJWT[crypto]>=2.13.0\n", {"pyjwt": "2.12.1"})

    with pytest.raises(RuntimeError) as e:
        assert_vendored_deps_fresh(req, deps)

    assert "build-deps.sh" in str(e.value)
    assert "pyjwt" in str(e.value)


def test_the_raising_wrapper_is_silent_on_a_healthy_tree(tmp_path):
    req, deps = _tree(tmp_path, "PyJWT[crypto]>=2.13.0\n", {"pyjwt": "2.13.0"})
    assert_vendored_deps_fresh(req, deps)  # must not raise


def test_the_real_agent_tree_is_fresh_when_present():
    """Runs for real locally (where `_deps` exists) and skips in CI.

    This is the only test that touches the actual artifact, and it is the one that
    would have caught the shipped defect before deploy.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    deps = root / "agent" / "_deps"
    if not deps.is_dir():
        pytest.skip("agent/_deps is gitignored and absent (CI / fresh clone)")
    assert stale_vendored_deps(root / "agent" / "requirements.txt", deps) == []
