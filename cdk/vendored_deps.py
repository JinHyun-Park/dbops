"""Synth-time gate: the vendored agent tree must satisfy its own requirements file.

WHY THIS EXISTS
---------------
`agent/_deps/` is what actually ships inside the AgentCore Runtime artifact. It is a
frozen tree produced by `agent/build-deps.sh`, and it is gitignored, so:

  - CI cannot see it (a fresh clone has no `_deps`), and
  - it never appears in a diff or a PR review.

Dependabot's pip config covers `/agent`, so it keeps RAISING the floors in
`agent/requirements.txt` without rebuilding the tree. Measured 2026-08-03: the file
already declared `PyJWT[crypto]>=2.13.0` and `mcp>=1.28.1` while the shipped tree held
pyjwt 2.12.1 and mcp 1.27.1, i.e. the deployed image contradicted its own requirements
file, carrying 20 known advisories. pyjwt is the one that matters: `agent/tenancy.py`
uses it to verify the caller's Cognito id_token against the pool JWKS, which is the
tenancy isolation boundary.

WHY AT SYNTH AND NOT IN CI
--------------------------
`deploy.sh` does run `build-deps.sh` first, so the full driver was never the hole. The
hole is `cdk deploy dbops-<env>-agent` on its own, which is the normal way to iterate on
one stack and skips the rebuild entirely. Synth is the one point both paths cross.

TWO FAILURE MODES, BOTH GUARDED
-------------------------------
1. STALE: `_deps` exists but sits below a floor declared in agent/requirements.txt.
2. ABSENT: no `_deps` at all. This is the state of every FRESH CLONE, because the tree is
   gitignored, and it is the worse of the two. Measured 2026-08-11 in a clean clone of the
   public repo: with `frontend/out` stubbed, `cdk synth` exited 0 with no `_deps`
   whatsoever. A deploy from that synth ships an AgentCore Runtime containing ZERO vendored
   dependencies, so the container cannot import strands / bedrock_agentcore / mcp / pyjwt
   and every chat turn dies at import. Nothing downstream catches it: the e2e suite asserts
   UI only and produces no runtime log events at all.

   An earlier version of this file treated ABSENT as "not a violation" so `tests/cdk` would
   pass in CI. That optimised for the CI path and silently broke the newcomer path, which is
   backwards for a repo published as self-service deployable. The default is now to REFUSE;
   the two CI synth paths opt out explicitly with DBOPS_SYNTH_WITHOUT_AGENT_DEPS=1, exactly
   as they already stub `frontend/out`.

`frontend/out` needs no guard here: its absence already fails synth loudly and by name
(`Cannot find asset at .../frontend/out/_next`, measured in the same clean clone).
"""

import os
import pathlib
import re

# `name`, optional `[extras]`, then a `>=` floor. Only `>=` is checked: it is the only
# operator this project uses, and an `==` pin cannot drift by definition.
_FLOOR_RE = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*>=\s*([0-9][^\s,;]*)")


def _norm(name: str) -> str:
    """PEP 503 style: dist-info dirs use `_` where requirements use `-`, and case
    varies (`PyJWT` on disk is `pyjwt-2.13.0.dist-info`)."""
    return name.lower().replace("-", "_")


def declared_floors(requirements_path) -> dict:
    """{normalized name: floor string} for every `>=` requirement."""
    floors = {}
    for line in pathlib.Path(requirements_path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _FLOOR_RE.match(line)
        if m:
            floors[_norm(m.group(1))] = m.group(2)
    return floors


def vendored_versions(deps_dir) -> dict:
    """{normalized name: version} read from the `*.dist-info` directory names.

    The dist-info name is the only record of what was installed: there is no lock
    file, because `build-deps.sh` resolves the floors at build time.
    """
    out = {}
    for d in pathlib.Path(deps_dir).glob("*.dist-info"):
        name, _, version = d.name[: -len(".dist-info")].rpartition("-")
        if name and version:
            out[_norm(name)] = version
    return out


def stale_vendored_deps(requirements_path, deps_dir) -> list:
    """Human-readable violations, empty only when the tree is present and consistent.

    Returns a list rather than raising so the caller decides the severity, and so the
    whole thing is unit-testable without a real 68MB `_deps` on disk.
    """
    deps_dir = pathlib.Path(deps_dir)
    if not deps_dir.is_dir():
        # A fresh clone, or anyone who skipped build-deps.sh. Refuse by default: see
        # "TWO FAILURE MODES" in the module docstring for why absence is worse than stale.
        # The opt-out exists for the two CI synth paths, which are structural checks that
        # never deploy. The name is deliberately long so it cannot be set by accident.
        if os.environ.get("DBOPS_SYNTH_WITHOUT_AGENT_DEPS") == "1":
            return []
        return [
            f"{deps_dir} DOES NOT EXIST: the agent would ship with zero vendored "
            "dependencies and every chat turn would fail at import"
        ]

    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - packaging ships with aws-cdk-lib
        return []

    have = vendored_versions(deps_dir)
    problems = []
    for pkg, floor in sorted(declared_floors(requirements_path).items()):
        got = have.get(pkg)
        if got is None:
            problems.append(
                f"{pkg}: declared >={floor} but NOT PRESENT in the vendored tree"
            )
            continue
        try:
            if Version(got) < Version(floor):
                problems.append(f"{pkg}: vendored {got} is BELOW the declared >={floor}")
        except InvalidVersion:
            # An unparseable version is reported, not silently passed: this gate exists
            # because a silent pass already shipped 20 advisories once.
            problems.append(f"{pkg}: cannot compare vendored {got!r} against >={floor}")
    return problems


def assert_vendored_deps_fresh(requirements_path, deps_dir) -> None:
    """Raise with the fix command when the vendored tree is stale."""
    problems = stale_vendored_deps(requirements_path, deps_dir)
    if not problems:
        return
    raise RuntimeError(
        "agent/_deps is MISSING or STALE relative to agent/requirements.txt, so this "
        "deploy would ship an agent image that cannot run:\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\n\nFix: bash agent/build-deps.sh   (or just run ./deploy.sh, which does it)\n"
        "A bare `cdk deploy` does NOT build the vendored tree. That is how it once went "
        "stale with 20 known advisories in it, pyjwt among them, and it is why a FRESH "
        "CLONE (where the gitignored tree does not exist at all) is refused here rather "
        "than allowed to deploy an agent with no dependencies.\n"
        "CI-only escape hatch, never for a deploy: DBOPS_SYNTH_WITHOUT_AGENT_DEPS=1"
    )
