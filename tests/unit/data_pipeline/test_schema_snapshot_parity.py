"""E-4 copy parity.

There is no Lambda layer in this repo: cross-asset sharing is verbatim file
copies plus a parity test (the engine_family.py / metric_filters.py convention).
Two families of copy exist here and BOTH matter for a different reason:

  schema_diff_util.py x3. The READER (get_schema_diff) computes its diff live
  while the PRODUCER stores diff_from_previous_json that get_schema_history and
  diagnose_root_cause replay. If the two computations drift, the same DDL event
  is described two different ways depending on which tool the agent called.

  schema_snapshot.py x2. One Aurora-MySQL collector serves RDS MySQL through
  MySQLDataApiAdapter, exactly as mysql_table_stats.py already does.

Byte-identity is asserted, and so is IDENTICAL RESULT on real inputs, because a
byte check alone would pass on three copies that are all equally wrong.
"""

import ast
import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]

# FOUR copies. The canonical one moved from operations/ to shared/ in the sixth
# pass over this surface: the contract is now read by the incident server too, and
# mcp_servers.shared is the only cross-server import root in this repo.
_UTIL_COPIES = {
    "canonical": _ROOT / "mcp-servers" / "mcp_servers" / "shared" / "schema_diff_util.py",
    "etl": _ROOT / "data-pipeline" / "etl_collector" / "collectors" / "schema_diff_util.py",
    "rds_direct": _ROOT / "data-pipeline" / "rds_direct_collector" / "schema_diff_util.py",
    # The dashboard schema-changes panel derives created/dropped from
    # schema_snapshots and needs the same SELECTION and the same compute_diff, so a
    # rename is a rename_candidate in the panel exactly as it is in get_schema_diff.
    # api/ cannot import mcp_servers, hence a copy.
    "api_dashboard": _ROOT / "api" / "dashboard" / "schema_diff_util.py",
}

_SNAPSHOT_COPIES = {
    "etl": _ROOT / "data-pipeline" / "etl_collector" / "collectors" / "schema_snapshot.py",
    "rds_direct": _ROOT / "data-pipeline" / "rds_direct_collector" / "schema_snapshot.py",
}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_schema_diff_util_copies_are_byte_identical():
    texts = {k: p.read_text() for k, p in _UTIL_COPIES.items()}
    canonical = texts["canonical"]
    drift = [k for k, t in texts.items() if t != canonical]
    assert not drift, (
        f"schema_diff_util.py copies drifted from the canonical: {drift}. "
        "Copy mcp-servers/mcp_servers/operations/schema_diff_util.py over them."
    )


def test_schema_snapshot_copies_are_byte_identical():
    texts = {k: p.read_text() for k, p in _SNAPSHOT_COPIES.items()}
    assert texts["etl"] == texts["rds_direct"], (
        "schema_snapshot.py drifted between etl_collector/collectors/ and "
        "rds_direct_collector/. One Aurora-MySQL collector serves RDS MySQL "
        "verbatim; copy one over the other."
    )


def test_every_copy_computes_the_same_diff():
    """Result parity, not just text parity. Real DDL: an add, a drop, an ALTER
    ADD COLUMN, and a rename pair."""
    before = {"users": ["email", "id"], "old_audit": ["id", "ts"], "legacy": ["k"]}
    after = {"users": ["email", "id", "phone"], "audit": ["id", "ts"], "brand_new": ["id"]}
    expected = {
        "added": ["brand_new"],
        "dropped": ["legacy"],
        "modified": [{"table": "users", "added_columns": ["phone"], "dropped_columns": []}],
        "rename_candidates": [{"from": "old_audit", "to": "audit"}],
    }
    for name, path in _UTIL_COPIES.items():
        mod = _load(path, f"_parity_util_{name}")
        assert mod.compute_diff(before, after) == expected, f"{name} disagrees"
        # The blob the Data API actually hands back is a STRING.
        assert mod.parse_tables('{"t": ["b", "a"]}') == {"t": ["a", "b"]}, name
        assert mod.diff_is_empty(mod.compute_diff(before, before)) is True, name


def test_reader_and_producer_agree_on_the_same_event():
    """The point of the copies: get_schema_diff's live computation and the diff
    the collector stores for get_schema_history must be the same object."""
    import sys

    sys.path.insert(0, str(_ROOT / "mcp-servers"))
    from mcp_servers.shared.schema_diff_util import compute_diff as reader_diff

    producer = _load(_UTIL_COPIES["etl"], "_parity_producer")
    before = {"orders": ["amount", "id"]}
    after = {"orders": ["amount", "currency", "id"]}
    assert reader_diff(before, after) == producer.compute_diff(before, after)


# ===========================================================================
# THE SELECTION CONTRACT, enforced. This is the section that would have failed
# every one of the SIX previous passes.
# ===========================================================================
# compute_diff was already shared. The SQL deciding WHICH TWO BLOBS ARE COMPARABLE
# was not: schema_diff.IMPLICIT_SQL, api/dashboard._SCHEMA_SNAPSHOT_PAIRS_SQL and
# the collector's PREV_SQL were three independent definitions, so every pass fixed
# the ones its file ownership happened to include and the defect survived in the
# rest. THREE mechanical rules replace the editorial one:
#
#   1. NO CONSUMER MAY WRITE `FROM schema_snapshots`. Every read is built from a
#      fragment in schema_diff_util.py, so a change to the comparability rule
#      reaches every consumer at once or reaches none of them.
#   2. NO CONSUMER MAY CALL compute_diff. `compare()` is the only licensed way to
#      obtain a diff and it cannot be called without the read_scope and the
#      per-schema confirmation state, so a consumer that wants a diff is forced to
#      select them.
#   3. EVERY FUNCTION THAT SELECTS ROWS CARRIES THE OBSERVATION. Rule 1 is
#      satisfied by building the statement from ALL_ROWS, and that is exactly what
#      api/dashboard `_timeline` did while getting no observation channel at all:
#      a FIFTH interpreter, living inside a file rules 1 and 2 had already cleared,
#      answering the same question as diagnose_root_cause. So the rule is
#      STATEMENT-SCOPED, per function, not per file.
#
# AND THE SET OF CONSUMERS IS DISCOVERED, NOT WRITTEN DOWN. The sixth pass wrote
# the two rules against a hand-written `_CONSUMERS` dict, which made them
# mechanical over exactly the files someone remembered. A new file was invisible,
# and so was a second interpreter inside a listed one, which is how `_timeline`
# escaped. `_discover_consumers()` below walks the shipped Python instead, so a new
# consumer fails this suite the day it appears.
#
# ALL THREE RULES RESOLVE NAMES THE SAME WAY, and that is the eighth pass. The
# seventh discovered consumers by an ImportFrom whose MODULE ends with
# schema_diff_util, and matched row sources, compute_diff calls and observation calls
# by ast.Name id. Every one of those signals is blind to reaching the contract
# through a MODULE OBJECT, which is one keyword away from the idiom the collectors
# already use, and TWO real files proved it while the full suite stayed at 2530
# passed:
#   data-pipeline/etl_collector/collectors/ddl_rollup.py
#     `import schema_diff_util` then `schema_diff_util.ALL_ROWS`, with ZERO
#     occurrences of the table name anywhere in the file.
#   mcp-servers/mcp_servers/incident/tools/ddl_near_incident.py
#     `from mcp_servers.shared import schema_diff_util`, where node.module is
#     "mcp_servers.shared" so the endswith check on it never fires.
# `_reaches_the_contract_module` covers all four import forms and `_referenced`
# covers attribute access, so the three rules see a module object exactly as they see
# a bare name. Both files were re-added as mutations after the widening and each one
# turns this suite red.

# Where shipped Python lives. tests/ is excluded on purpose (a test HAS to be able
# to write raw SQL to prove what the shipped statement does), as is cdk/ (schemas,
# no reads).
_SCAN_ROOTS = ("api", "mcp-servers", "data-pipeline", "agent", "tools")
_SKIP_DIRS = {"node_modules", "__pycache__", "_deps", ".git", "cdk.out", "build"}

# THE EXPLICIT NON-INTERPRETER ALLOWLIST, and every entry needs a reason, because
# an allowlist is the one place a seventh pass could still hide.
_NOT_INTERPRETERS = {
    # The contract itself. It OWNS the row sources; binding it to its own rule is
    # circular. Its four copies are byte-identity-checked above instead.
    "mcp-servers/mcp_servers/shared/schema_diff_util.py": "the contract",
    "api/dashboard/schema_diff_util.py": "the contract (verbatim copy)",
    "data-pipeline/etl_collector/collectors/schema_diff_util.py":
        "the contract (verbatim copy)",
    "data-pipeline/rds_direct_collector/schema_diff_util.py":
        "the contract (verbatim copy)",
    # Retention. A DELETE interprets nothing and answers no question about what
    # changed, so binding it to a comparability fragment would say something false
    # about what it does. Its own guard is
    # tests/unit/data_pipeline/test_etl_purge.py, which EXECUTES it on a real
    # engine and asserts which rows survive.
    "data-pipeline/etl_collector/handler.py": "the retention purge, interprets nothing",
}

# The two row sources a consumer builds a statement from. A function that reaches
# either of them is SELECTING SNAPSHOT ROWS to answer a question, which is what
# obliges it to also carry the observation. COVERAGE_SQL / OBSERVED_SQL /
# ESTABLISHED_SCOPE_SQL are complete statements owned by the contract, not row
# sources a consumer composes, so they are not in here.
_ROW_SOURCES = {"SCOPED_ROWS", "ALL_ROWS"}

# The two shared entry points to the confirmation state. `compare()` needs one, and
# so does every negative any consumer states about a cluster.
_OBSERVATION_ENTRIES = {"observed", "observation_state"}

# THE PRODUCERS, exempt from rule 3 and the licensed compute_diff callers. They
# hold the LIVE READ, so they ARE the scope, and there is no stored confirmation
# state to consult yet: they are what produces it.
_PRODUCERS = {
    "data-pipeline/etl_collector/collectors/schema_snapshot.py",
    "data-pipeline/rds_direct_collector/schema_snapshot.py",
}


def _module_string_constants(tree):
    """Every string CONSTANT in the tree except docstrings (see _sql_literals)."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


def _referenced(node):
    """Every name the code under `node` reaches, by BARE NAME or through a MODULE
    OBJECT.

    `schema_diff_util.ALL_ROWS` is an ast.Attribute, not an ast.Name, so a walk that
    collects only Name ids cannot see the module-object idiom, and the module-object
    idiom is what the collectors in data-pipeline/etl_collector/collectors/ already
    use for their siblings. That single keyword is what let both eighth-pass escape
    files through three separate rules at once: discovery, the compute_diff rule and
    the observation rule all resolved names the same way.

    An attribute is matched by its ATTRIBUTE NAME regardless of what it hangs off, so
    `sdu.ALL_ROWS`, `schema_diff_util.ALL_ROWS` and a re-export all count. That is
    deliberately loose in the safe direction: an extra match makes a rule fire on a
    file it did not have to, which is a review-visible failure, while a miss is a
    consumer nobody is checking.
    """
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _reaches_the_contract_module(tree):
    """True when the file imports schema_diff_util in ANY of the four forms Python
    offers. The first two were the only ones checked, and they are the two that
    import names FROM the module; the last two reach the MODULE OBJECT, which is
    where the eighth-pass escapes lived:

      from schema_diff_util import ALL_ROWS                    module endswith
      from mcp_servers.shared.schema_diff_util import ALL_ROWS  module endswith
      import schema_diff_util                                  ast.Import
      from mcp_servers.shared import schema_diff_util           alias, module is
                                                               "mcp_servers.shared"
    Both missed forms were proved with real files that the FULL SUITE then passed:
    data-pipeline/etl_collector/collectors/ddl_rollup.py building its statement from
    `schema_diff_util.ALL_ROWS` with ZERO occurrences of the table name, and
    mcp-servers/mcp_servers/incident/tools/ddl_near_incident.py doing the same
    through `from mcp_servers.shared import schema_diff_util`.

    `from . import schema_diff_util` (module None) is covered by the alias branch,
    and a dotted `import a.b.schema_diff_util` by the same endswith on the alias.
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # The module a name is imported FROM (ImportFrom only).
            if getattr(node, "module", None) and node.module.endswith("schema_diff_util"):
                return True
            # The module imported AS A NAME. `import x.y.schema_diff_util` and
            # `from pkg import schema_diff_util` are both here, and neither is an
            # ImportFrom whose `module` says anything about the contract.
            if any(a.name.endswith("schema_diff_util") for a in node.names):
                return True
    return False


def _discover_consumers():
    """{repo-relative path: Path} for every shipped file that reads schema_snapshots.

    TWO ways to be one, because either alone misses a real consumer:
      * a string CONSTANT naming the table (a file writing its own SQL). Comments
        and docstrings do not count: four of these files explain the rule in prose
        and one of them, pg_table_stats.py, only MENTIONS the table.
      * an import of schema_diff_util in any form (a file building its statement
        from the contract). A consumer doing this correctly never contains the
        string "schema_snapshots" at all, which is precisely how a text grep would
        miss the next one.
    """
    found = {}
    for root in _SCAN_ROOTS:
        for path in sorted((_ROOT / root).rglob("*.py")):
            if _SKIP_DIRS & set(path.parts):
                continue
            rel = path.relative_to(_ROOT).as_posix()
            if rel in _NOT_INTERPRETERS:
                continue
            text = path.read_text()
            if "schema_snapshots" not in text and "schema_diff_util" not in text:
                continue
            tree = ast.parse(text)
            if (_reaches_the_contract_module(tree)
                    or any("schema_snapshots" in lit
                           for lit in _module_string_constants(tree))):
                found[rel] = path
    return found


_CONSUMERS = _discover_consumers()

# The six that exist today. Asserted so a discovery walk that silently finds
# NOTHING cannot make every rule below vacuously true, which is the failure mode of
# a mechanical test: it would go green on the exact commit it is meant to fail.
_KNOWN_CONSUMERS = {
    "mcp-servers/mcp_servers/operations/tools/schema_diff.py",
    "mcp-servers/mcp_servers/operations/tools/schema_history.py",
    "mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py",
    "api/dashboard/handler.py",
    "data-pipeline/etl_collector/collectors/schema_snapshot.py",
    "data-pipeline/rds_direct_collector/schema_snapshot.py",
}


def test_the_consumer_set_is_discovered_and_finds_the_ones_we_know_about():
    """A new consumer joins this suite by EXISTING, and the known six prove the
    walk is not silently empty. A file discovered here that should not be bound by
    the rules goes in _NOT_INTERPRETERS with a reason, which is a review-visible
    edit; a file that nobody adds anywhere is now impossible."""
    missing = _KNOWN_CONSUMERS - set(_CONSUMERS)
    assert not missing, f"discovery no longer finds {sorted(missing)}"
    assert set(_NOT_INTERPRETERS) <= {
        p.relative_to(_ROOT).as_posix()
        for root in _SCAN_ROOTS for p in (_ROOT / root).rglob("*.py")}, (
        "an allowlist entry names a file that no longer exists, so it is silently "
        "excluding nothing and hiding whatever replaced it")


def _sql_literals(path):
    """Every string CONSTANT in the module except docstrings.

    Docstrings and comments are excluded on purpose: this rule is about the SQL a
    consumer SENDS, and the four files here have to be able to explain the rule in
    prose without tripping it. A comment is not an ast.Constant at all; a docstring
    is, so it is subtracted explicitly.
    """
    return _module_string_constants(ast.parse(path.read_text()))


def _row_source_readers(path):
    """{function name: the row sources it reaches} for one consumer file.

    STATEMENT-SCOPED, and that is the whole of FINDING 5. A module-level constant
    built from ALL_ROWS is itself a row source (resolved transitively), so a
    function using `_SCHEMA_SNAPSHOT_PAIRS_SQL` counts exactly as much as one
    inlining the fragment, and TWO functions in one file are two separate
    obligations rather than one satisfied file.
    """
    tree = ast.parse(path.read_text())
    derived = set(_ROW_SOURCES)
    # Transitive over module-level assignments. Two passes is one more than this
    # repo's deepest chain (fragment -> statement -> re-export). `_referenced`, not a
    # Name-only walk: `X = schema_diff_util.ALL_ROWS + "..."` builds a row source
    # through a module object and a Name walk sees nothing in it.
    for _ in range(3):
        for node in tree.body:
            if isinstance(node, ast.Assign):
                if _referenced(node.value) & derived:
                    derived |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    out = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        hit = _referenced(fn) & derived
        if hit:
            out[fn.name] = hit
    return out


@pytest.mark.parametrize("name", sorted(_CONSUMERS))
def test_no_consumer_selects_its_own_schema_snapshot_rows(name):
    """THE TEST THAT WOULD HAVE CAUGHT ALL FIVE PREVIOUS PASSES.

    Each of them left at least one consumer selecting its own rows, and the
    comparability rule they were fixing therefore applied to some consumers and not
    others. Most recently: the fifth pass added read_scope and taught the collector
    and the two MCP readers to respect it, while
    api/dashboard._SCHEMA_SNAPSHOT_PAIRS_SQL and schema_diff.IMPLICIT_SQL still
    recomputed diffs straight from the blobs, so the phantom mass DROP was still
    live in both of them.

    Run this test against any of those five commits and it fails there.
    """
    offenders = [lit for lit in _sql_literals(_CONSUMERS[name])
                 if "FROM schema_snapshots" in lit or "JOIN schema_snapshots" in lit]
    assert not offenders, (
        f"{name} selects schema_snapshots rows itself. Build the statement from "
        "SCOPED_ROWS (comparing two blobs: must be scope-equal), ALL_ROWS "
        "(replaying stored diffs, or counting what exists) or "
        "LATEST_SCOPED_TIME_SUBQUERY in schema_diff_util.py instead. Duplicating "
        "the selection is how one defect survived six passes.\n"
        f"offending literals: {offenders}"
    )


@pytest.mark.parametrize("name", sorted(_CONSUMERS))
def test_no_consumer_computes_a_diff_without_the_facts_that_qualify_it(name):
    """`compare()` requires the read_scope and the observation; compute_diff does
    not. A consumer calling compute_diff directly is a consumer holding a bare diff
    with no way to know whether the two blobs described the same catalog, which is
    the state every previous pass ended in.

    The COLLECTOR is the one legitimate caller: it holds the live read itself, so
    it IS the scope, and there is no stored confirmation state to consult yet.
    """
    src = _CONSUMERS[name].read_text()
    tree = ast.parse(src)
    # Both call shapes. `compute_diff(...)` is a Name and `schema_diff_util
    # .compute_diff(...)` is an Attribute, and the second escaped this rule
    # entirely: a file that reached the contract through the module object could
    # call the unlicensed function and nothing here saw it.
    called = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name):
            called.add(n.func.id)
        elif isinstance(n.func, ast.Attribute):
            called.add(n.func.attr)
    if name in _PRODUCERS:
        assert "compute_diff" in called, (
            "the producer compares this read against the stored blob, so it is the "
            "one legitimate compute_diff caller; if that call is gone, the "
            "store-on-change decision has moved somewhere this test cannot see"
        )
        return
    assert "compute_diff" not in called, (
        f"{name} calls compute_diff directly. Call compare(), which cannot be "
        "called without the read_scope the pair was recorded under and the "
        "per-schema confirmation state. A diff that travels without those two is "
        "what five passes handed the next consumer."
    )


@pytest.mark.parametrize("name", sorted(_CONSUMERS))
def test_every_function_that_selects_snapshot_rows_carries_the_observation(name):
    """RULE 3, and THE TEST THAT WOULD HAVE CAUGHT THE SIXTH PASS.

    Rules 1 and 2 are satisfied by building the statement from ALL_ROWS and not
    calling compute_diff, and api/dashboard `_timeline` did both while getting no
    observation channel at all. It is a FIFTH interpreter of these rows, answering
    the same question as diagnose_root_cause (did any DDL land near this incident),
    living inside a file the sixth pass had already cleared at FILE scope. So the
    obligation is per FUNCTION: whatever selects the rows also has to know whether
    the schemas behind them could still be seen, or its empty result reads as
    "no DDL happened" over schemas nobody looked at.

    The producers are exempt: they hold the live read, so they ARE the observation.
    """
    readers = _row_source_readers(_CONSUMERS[name])
    assert readers, (
        f"{name} was discovered as a consumer but reaches no row source. Either it "
        "writes its own SQL (rule 1 says where that belongs) or it is not an "
        "interpreter at all and belongs in _NOT_INTERPRETERS with a reason."
    )
    if name in _PRODUCERS:
        return
    src = ast.parse(_CONSUMERS[name].read_text())
    blind = {}
    for fn in ast.walk(src):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name not in readers:
            continue
        # `_referenced`, so `schema_diff_util.observed(...)` satisfies the rule the
        # same way a bare `observed(...)` does. Without it a file reaching the
        # contract through the module object fails this rule even when it DOES carry
        # the observation, and a false failure is what gets a rule weakened.
        if not _referenced(fn) & _OBSERVATION_ENTRIES:
            blind[fn.name] = sorted(readers[fn.name])
    assert not blind, (
        f"{name}: {blind} select snapshot rows without reaching the observation. "
        f"Call one of {sorted(_OBSERVATION_ENTRIES)} in the same function and put "
        "the result in the payload, the way _schema_changes and "
        "_collect_schema_changes do. A statement built from the shared fragment is "
        "only half the contract: the other half is being able to say which schemas "
        "the answer does NOT cover."
    )


def test_every_copy_exposes_the_whole_contract():
    """Byte-identity already implies this. It is asserted by NAME anyway, because a
    consumer importing a fragment that a sibling copy does not export fails at
    runtime in one Lambda and nowhere in this suite."""
    contract = ("SCOPED_ROWS", "ALL_ROWS", "LATEST_SCOPED_TIME_SUBQUERY",
                "ESTABLISHED_SCOPE_SQL", "OBSERVED_SQL", "COVERAGE_SQL",
                "CLUSTER_ENGINE_SQL", "CONFIRM_WITHIN_SEC", "OBSERVATION_STATUSES",
                "DROPPED_CAVEAT", "UNSUPPORTED_ENGINE", "UNSUPPORTED_DIALECT_NOTE",
                "compare", "observed", "observation_is_complete", "not_seen_note",
                "snapshot_dialect_supported",
                "parse_tables", "compute_diff", "diff_is_empty",
                "SchemaComparison")
    for name, path in _UTIL_COPIES.items():
        mod = _load(path, f"_parity_contract_{name}")
        missing = [c for c in contract if not hasattr(mod, c)]
        assert not missing, f"{name} is missing {missing}"


def test_a_null_scope_is_comparable_to_nothing_in_every_copy():
    """The one-line version of FINDING 1. `read_scope = :read_scope` never matches
    NULL by SQL's own rules, and that is the whole migration safety property: a
    pre-v27 row is not silently adopted as a comparison partner. A copy that grew
    an `IS NOT DISTINCT FROM` or a COALESCE default would turn every pre-v27 row
    into a wildcard, which is the phantom mass DROP."""
    for name, path in _UTIL_COPIES.items():
        mod = _load(path, f"_parity_scope_{name}")
        assert "read_scope = :read_scope" in mod.SCOPED_ROWS, name
        for banned in ("IS NOT DISTINCT FROM", "COALESCE(read_scope"):
            assert banned not in mod.SCOPED_ROWS, (name, banned)
        # And compare() refuses rather than falling back.
        with pytest.raises(ValueError):
            mod.compare("public", '{"t": ["a"]}', "{}", read_scope="",
                        observation={})


def test_the_dialect_gate_is_positive_and_fail_closed_in_every_copy():
    """FINDING 4 of the seventh pass. Every claim on this surface rests on "absent
    from the catalog read means absent from the database", and that is FALSE on
    MySQL: measured on 9.3.0, a table-level REVOKE removes the table from the read, a
    column-level GRANT removes a column, a database-level REVOKE removes the schema
    entirely, and read_scope (CURRENT_USER()) is identical across all of it. So the
    product refuses rather than adding a predicate.

    POSITIVE and FAIL-CLOSED: only an engine that positively says postgres passes,
    and an engine we could not RESOLVE is `unavailable` (we cannot decide) rather
    than either answer.

    And the SENTENCE is about the rule, not about MySQL. The gate is False for five
    families (mysql, sqlserver, documentdb, dynamodb, elasticache) and MySQL's
    measured mechanism is only MySQL's, so the shared note states what IS collected
    and why; the per-engine grounds live on the predicate's docstring.
    """
    for name, path in _UTIL_COPIES.items():
        mod = _load(path, f"_parity_dialect_{name}")
        for engine in ("aurora-postgresql", "postgres", "PostgreSQL", "aurora-postgresql-15"):
            assert mod.snapshot_dialect_supported(engine) is True, (name, engine)
        for engine in ("aurora-mysql", "mysql", "MySQL", "sqlserver-se", "docdb",
                       "redis", "dynamodb", "", None, "unknown"):
            assert mod.snapshot_dialect_supported(engine) is False, (name, engine)

        rows = [{"schema_name": "app", "read_scope": "db/1", "last_seen": "2026-07-29",
                 "holds_tables": "y", "age_sec": 60}]

        def query(sql, params=None, _engine=None, _rows=rows):
            if "FROM cluster_meta" in sql:
                return [] if _engine is None else [{"engine": _engine}]
            if "read_scope IS NOT NULL" in sql:
                return [{"read_scope": "db/1"}]
            return _rows

        # A refused dialect: REFUSED, and it never reads as an absence of change.
        obs = mod.observed(lambda s, p=None: query(s, p, "aurora-mysql"), "c1")
        assert obs["status"] == mod.UNSUPPORTED_ENGINE, (name, obs)
        assert mod.observation_is_complete(obs) is False, name
        note = mod.not_seen_note(obs)
        assert note == mod.UNSUPPORTED_DIALECT_NOTE, (name, note)
        # It states the POSITIVE rule and names the catalog the rule is about.
        assert "PostgreSQL" in note and "pg_namespace" in note, (name, note)
        # ...and it is NOT dressed up as a young cluster, which would promise a
        # baseline on the next ETL cycle that is never coming.
        assert obs["status"] != "no_snapshots"

        # FINDING 4 OF THE EIGHTH PASS. The gate above is False for five
        # engine families and the sentence used to explain MySQL's
        # privilege-filtered information_schema, so a DocumentDB or DynamoDB
        # operator was handed a reason that is not their reason. ONE sentence for
        # all of them means it may not name any single family's mechanism.
        for other in ("docdb", "documentdb", "dynamodb", "redis", "valkey",
                      "memcached", "sqlserver-se", "sqlserver-ex", "mysql"):
            o = mod.observed(lambda s, p=None, e=other: query(s, p, e), "c1")
            assert o["status"] == mod.UNSUPPORTED_ENGINE, (name, other, o)
            n = mod.not_seen_note(o)
            assert n == mod.UNSUPPORTED_DIALECT_NOTE, (name, other)
            for one_familys_reason in ("MySQL", "REVOKE", "information_schema",
                                       "DocumentDB", "DynamoDB", "Redis",
                                       "SQL Server"):
                assert one_familys_reason not in n, (name, other, one_familys_reason)

        # An engine nobody could resolve is a THIRD state: we could not decide.
        unknown = mod.observed(lambda s, p=None: query(s, p, None), "c1")
        assert unknown["status"] == "unavailable", (name, unknown)
        assert mod.UNSUPPORTED_DIALECT_NOTE not in mod.not_seen_note(unknown), name

        # And the supported dialect still answers.
        ok = mod.observed(lambda s, p=None: query(s, p, "aurora-postgresql"), "c1")
        assert ok["status"] == "fresh", (name, ok)

        # The engine lookup is the FIRST thing it does: a refused dialect must not
        # depend on the snapshot reads working at all.
        def only_engine(sql, params=None):
            if "FROM cluster_meta" in sql:
                return [{"engine": "mysql"}]
            raise RuntimeError("the snapshot reads must not be reached")
        assert mod.observed(only_engine, "c1")["status"] == mod.UNSUPPORTED_ENGINE, name


def test_confirmation_is_per_schema_and_never_a_cluster_wide_max():
    """FINDING 3, pinned in the SQL. The previous shape asked "is this schema the
    most recently seen one in the cluster", a RELATIVE test: with every schema
    sharing one stamp (which is what the collector writes, one run timestamp per
    cycle) NONE was ever unconfirmed, however old the stamp. Measured pre-fix
    against a frozen cycle: collector `not_seen 2 ["alpha","public"]`, readers
    `unconfirmed_schemas []`."""
    for name, path in _UTIL_COPIES.items():
        mod = _load(path, f"_parity_obs_{name}")
        assert "MAX(y.last_seen_at)" not in mod.OBSERVED_SQL, name
        assert "NOW() - last_seen_at" in mod.OBSERVED_SQL, name
        # Driven, not read: two schemas with the IDENTICAL stale stamp must both
        # come back unconfirmed.
        stale = 40 * 24 * 3600
        rows = [{"schema_name": n, "read_scope": "db/1", "last_seen": "2026-01-01",
                 "holds_tables": "y", "age_sec": stale} for n in ("alpha", "beta")]

        def query(sql, params=None, _rows=rows):
            if "FROM cluster_meta" in sql:
                return [{"engine": "aurora-postgresql"}]
            if "read_scope IS NOT NULL" in sql:
                return [{"read_scope": "db/1"}]
            return _rows
        obs = mod.observed(query, "c1")
        assert obs["status"] == "not_seen", (name, obs)
        assert obs["unconfirmed_schemas"] == ["alpha", "beta"], (name, obs)
        assert mod.observation_is_complete(obs) is False, name


def test_observation_statuses_are_exactly_what_observed_can_return():
    """A signal VALUE that appears in no enumeration is how every pass escaped, so
    the enumeration is derived from the function rather than written beside it."""
    mod = _load(_UTIL_COPIES["canonical"], "_parity_statuses")
    src = _UTIL_COPIES["canonical"].read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "observed")
    returned = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in mod.OBSERVATION_STATUSES:
                returned.add(node.value)
        # ...and by NAME, because a status can be returned through a module constant
        # (`{"status": UNSUPPORTED_ENGINE}`). Resolving the name is what keeps this
        # test derived from the function rather than from its spelling: without it,
        # naming a constant silently removed a value from the enumeration.
        if isinstance(node, ast.Name):
            val = getattr(mod, node.id, None)
            if isinstance(val, str) and val in mod.OBSERVATION_STATUSES:
                returned.add(val)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "status" for t in node.targets):
            if isinstance(node.value, ast.Constant):
                returned.add(node.value.value)
    assert returned == set(mod.OBSERVATION_STATUSES), (
        f"observed() can return {sorted(returned)} but OBSERVATION_STATUSES is "
        f"{sorted(mod.OBSERVATION_STATUSES)}. Every value has to be in the "
        "enumeration or the panel's state matrix cannot see it, which is exactly "
        "how `not_seen` reached the operator as 'no changes detected'."
    )


def test_last_confirmed_survives_a_cluster_that_has_gone_entirely_unconfirmed():
    """Driven on the post-fix code of this very commit, because the first draft got
    it wrong: `last_confirmed` was collected only from schemas still INSIDE the
    freshness bar, so a cluster whose whole last cycle was 30 minutes ago reported
    `last_confirmed: None` and the operator lost the one number the sentence needs.

    A stamp made under the ESTABLISHED scope is a real confirmation of that schema at
    that moment whether or not it is still fresh. Only the scopes that confirm
    nothing NOW (another scope, or no scope at all) are excluded.
    """
    for name, path in _UTIL_COPIES.items():
        mod = _load(path, f"_parity_lastconf_{name}")
        rows = [{"schema_name": "alpha", "read_scope": "db/1",
                 "last_seen": "2026-07-29 04:08:15+09", "holds_tables": "y",
                 "age_sec": 30 * 60},
                {"schema_name": "beta", "read_scope": "db/1",
                 "last_seen": "2026-07-29 03:00:00+09", "holds_tables": "y",
                 "age_sec": 90 * 60}]

        def query(sql, params=None, _rows=rows):
            if "FROM cluster_meta" in sql:
                return [{"engine": "aurora-postgresql"}]
            if "read_scope IS NOT NULL" in sql:
                return [{"read_scope": "db/1"}]
            return _rows
        obs = mod.observed(query, "c1")
        assert obs["status"] == "not_seen", (name, obs)
        assert obs["last_confirmed"] == "2026-07-29 04:08:15+09", (name, obs)
        # and a row under ANOTHER scope contributes no confirmation at all
        rows.append({"schema_name": "gamma", "read_scope": "other/2",
                     "last_seen": "2027-01-01 00:00:00+09", "holds_tables": "y",
                     "age_sec": 60})
        obs = mod.observed(query, "c1")
        assert obs["last_confirmed"] == "2026-07-29 04:08:15+09", (name, obs)
        assert "gamma" in obs["unconfirmed_schemas"], (name, obs)


def test_the_not_seen_sentence_dates_each_schema_separately():
    """Also driven against this commit's own first draft, which put ONE
    cluster-level timestamp in the sentence: a pre-v27 history plus one read of
    another database then reported "마지막 확인 시각은 <that read's time>" for two
    schemas that read never touched. They were last confirmed at different times, or
    never, so the sentence says so per schema."""
    for name, path in _UTIL_COPIES.items():
        mod = _load(path, f"_parity_note_{name}")
        obs = {
            "status": "not_seen",
            "last_confirmed": "2026-07-29 04:00:00+09",
            "unconfirmed_schemas": ["billing", "core"],
            "schemas": {
                "billing": {"confirmation": mod.UNMIGRATED, "last_seen": None,
                            "holds_tables": True},
                "core": {"confirmation": mod.NOT_SEEN,
                         "last_seen": "2026-07-19 04:00:00+09", "holds_tables": True},
            },
        }
        note = mod.not_seen_note(obs)
        assert "billing(마지막 확인 기록 없음)" in note, (name, note)
        assert "core(마지막 확인 2026-07-19 04:00:00+09)" in note, (name, note)
        # the cluster-level stamp must NOT be attributed to either of them
        assert "2026-07-29 04:00:00+09" not in note, (name, note)
        # and it is never phrased as a drop
        for word in ("삭제됨", "dropped", "DROP"):
            assert word not in note, (name, word)
        assert "삭제로 단정하지 않고" in note, name
        # a fully confirmed cluster gets no sentence at all
        assert mod.not_seen_note({"status": "fresh", "unconfirmed_schemas": []}) == ""
