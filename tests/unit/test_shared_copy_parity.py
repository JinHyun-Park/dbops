"""Parity guard for EVERY verbatim copy of a mcp_servers/shared module.

`api/` and `data-pipeline/` Lambda code assets are sandboxed per-function and
cannot import `mcp_servers`, so shared logic is duplicated by file copy plus a
test. That contract only holds where a test actually enforces it, and it did
not: before this file, only 4 of the 13 mirrored modules were guarded
(engine_family, metric_filters, schema_diff_util, rds_instance_pricing). The
unguarded ones included `upgrade_estimator.py` and `ddl_estimator.py`, whose own
docstrings claim they are "mirrored byte-for-byte into api/simulation/" while
nothing checked it.

Pairs are DISCOVERED, not listed: any non-empty `*.py` under
mcp-servers/mcp_servers/shared/ whose basename also appears under `api/` or
`data-pipeline/` is a mirror and must match. A hand-maintained list would go
stale the moment someone adds a copy, which is exactly the failure this guards.

Equivalence rule, same as the rds_instance_pricing guard this replaces: only the
leading MODULE DOCSTRING may differ (some replicas carry an extra NOTE about the
no-mcp_servers rule). Everything after it, imports and all code and all
comments, must be identical. Comments are deliberately IN scope: a comment that
explains a rule in one copy and not the other is how a maintainer edits one side
and not the other.

The stricter per-module guards stay: engine_family's asserts the four copies
CLASSIFY identically, which is a behavioural claim this file does not make.
"""

import ast
import pathlib

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SHARED = _REPO / "mcp-servers/mcp_servers/shared"
_MIRROR_ROOTS = ("api", "data-pipeline")


def _code_after_docstring(path: pathlib.Path) -> str:
    """File text with ONLY the leading module docstring removed.

    Uses the AST to locate the docstring's exact end line rather than scanning
    for a triple-quote delimiter, which a triple-quote inside code could fool.
    """
    text = path.read_text()
    body = ast.parse(text).body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)):
        return "\n".join(text.splitlines()[body[0].end_lineno:])
    return text  # no module docstring -> compare whole file


def _mirror_pairs():
    """[(name, canonical_path, [replica_paths])] for every discovered mirror.

    Empty files are skipped: every package has an empty `__init__.py`, and those
    are package markers rather than shared logic.
    """
    canon = {
        p.name: p
        for p in sorted(_SHARED.glob("*.py"))
        if p.read_text().strip()
    }
    found = {}
    for root in _MIRROR_ROOTS:
        for p in sorted((_REPO / root).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            if p.name in canon:
                found.setdefault(p.name, []).append(p)
    return [(name, canon[name], reps) for name, reps in sorted(found.items())]


def test_mirror_discovery_finds_the_known_copies():
    """The discovery itself must not silently find nothing.

    A typo in the glob or a moved directory would make every parity assertion
    below vacuously pass, which is the failure mode this whole file exists to
    prevent. These names are the modules whose docstrings state the mirror
    contract, so their absence means discovery broke, not that a copy was
    legitimately removed.
    """
    names = {name for name, _, _ in _mirror_pairs()}
    for expected in (
        "engine_family.py",
        "metric_filters.py",
        "schema_diff_util.py",
        "upgrade_estimator.py",
        "ddl_estimator.py",
        "rds_instance_pricing.py",
    ):
        assert expected in names, (
            f"mirror discovery did not find {expected}; the glob or the layout "
            "changed and every parity check below would pass vacuously"
        )


def test_every_shared_copy_matches_its_canonical():
    drifted = []
    for name, canonical, replicas in _mirror_pairs():
        want = _code_after_docstring(canonical)
        for replica in replicas:
            if _code_after_docstring(replica) != want:
                drifted.append(f"{replica.relative_to(_REPO)} != shared/{name}")
    assert not drifted, (
        "shared-module copies diverged below the module docstring:\n  "
        + "\n  ".join(drifted)
        + "\n`api/` and `data-pipeline/` cannot import mcp_servers, so these are "
        "file copies: a change to one MUST be mirrored into the others."
    )


# --- copies that have NO canonical in shared/ ---------------------------------
# Some modules are duplicated between sibling Lambdas without any copy living in
# mcp_servers/shared: tenancy.py exists 10 times across api/, the mysql_* deep
# readers twice (etl_collector + rds_direct_collector), ws_notify.py twice. Those
# had no guard at all, and tenancy.py is the visibility boundary: one drifted copy
# is one REST route that stops scoping rows to the caller's team.
#
# These are byte-identical including the docstring (measured), so the rule here
# is stricter than the shared-mirror rule above, which tolerates a docstring NOTE.

def _duplicate_families():
    """{basename: [paths]} for every non-handler module appearing 2+ times.

    `handler.py` and `__init__.py` are excluded: every Lambda has its own
    handler, and they are deliberately different.
    """
    seen = {}
    for root in _MIRROR_ROOTS:
        for p in sorted((_REPO / root).rglob("*.py")):
            if "__pycache__" in p.parts or p.name in ("handler.py", "__init__.py"):
                continue
            if not p.read_text().strip():
                continue
            seen.setdefault(p.name, []).append(p)
    return {name: paths for name, paths in seen.items() if len(paths) > 1}


def test_duplicate_discovery_finds_the_known_families():
    """Guard the guard: a broken glob would make the assertion below vacuous."""
    families = _duplicate_families()
    assert "tenancy.py" in families, "tenancy.py copies not discovered"
    assert len(families["tenancy.py"]) >= 2, "tenancy.py should have sibling copies"


def test_duplicated_modules_without_a_shared_canonical_are_identical():
    drifted = []
    for _name, paths in sorted(_duplicate_families().items()):
        first = paths[0]
        want = first.read_text()
        for other in paths[1:]:
            if other.read_text() != want:
                drifted.append(
                    f"{other.relative_to(_REPO)} != {first.relative_to(_REPO)}"
                )
    assert not drifted, (
        "sibling module copies diverged:\n  "
        + "\n  ".join(drifted)
        + "\nThese are verbatim duplicates across sandboxed Lambda assets. If a "
        "divergence is intentional, the copies need different names, because "
        "nothing else tells the next maintainer which one to edit."
    )
