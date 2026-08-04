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

A missing `_deps` is NOT an error here: that is the CI/fresh-clone state, and failing on
it would break `tests/cdk`. The condition being guarded is a tree that EXISTS and is
stale, which is the only way a bad artifact reaches AgentCore.
"""

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
    """Human-readable violations, empty when the tree is consistent or absent.

    Returns a list rather than raising so the caller decides the severity, and so the
    whole thing is unit-testable without a real 68MB `_deps` on disk.
    """
    deps_dir = pathlib.Path(deps_dir)
    if not deps_dir.is_dir():
        # CI / fresh clone. Not a violation: see the module docstring.
        return []

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
        "agent/_deps is STALE relative to agent/requirements.txt, so this deploy would "
        "ship an image that contradicts its own declared dependencies:\n"
        + "\n".join(f"  - {p}" for p in problems)
        + "\n\nFix: bash agent/build-deps.sh\n"
        "(deploy.sh does this automatically; a bare `cdk deploy` does not, which is how "
        "the tree went stale with 20 known advisories in it, pyjwt among them.)"
    )
