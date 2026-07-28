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
# every one of the five previous passes.
# ===========================================================================
# compute_diff was already shared. The SQL deciding WHICH TWO BLOBS ARE COMPARABLE
# was not: schema_diff.IMPLICIT_SQL, api/dashboard._SCHEMA_SNAPSHOT_PAIRS_SQL and
# the collector's PREV_SQL were three independent definitions, so every pass fixed
# the ones its file ownership happened to include and the defect survived in the
# rest. Two mechanical rules replace the editorial one:
#
#   1. NO CONSUMER MAY WRITE `FROM schema_snapshots`. Every read is built from a
#      fragment in schema_diff_util.py, so a change to the comparability rule
#      reaches all four consumers at once or reaches none of them.
#   2. NO CONSUMER MAY CALL compute_diff. `compare()` is the only licensed way to
#      obtain a diff and it cannot be called without the read_scope and the
#      per-schema confirmation state, so a consumer that wants a diff is forced to
#      select them.

# Every file that reads schema_snapshots to answer a question about it. The purge
# in data-pipeline/etl_collector/handler.py is deliberately NOT here: it is a
# retention DELETE, it interprets nothing, and binding it to a comparability
# fragment would say something false about what it does.
_CONSUMERS = {
    "mcp_schema_diff":
        _ROOT / "mcp-servers/mcp_servers/operations/tools/schema_diff.py",
    "mcp_schema_history":
        _ROOT / "mcp-servers/mcp_servers/operations/tools/schema_history.py",
    "mcp_diagnose_root_cause":
        _ROOT / "mcp-servers/mcp_servers/incident/tools/diagnose_root_cause.py",
    "api_dashboard_handler": _ROOT / "api" / "dashboard" / "handler.py",
    "etl_collector":
        _ROOT / "data-pipeline/etl_collector/collectors/schema_snapshot.py",
    "rds_direct_collector":
        _ROOT / "data-pipeline/rds_direct_collector/schema_snapshot.py",
}


def _sql_literals(path):
    """Every string CONSTANT in the module except docstrings.

    Docstrings and comments are excluded on purpose: this rule is about the SQL a
    consumer SENDS, and the four files here have to be able to explain the rule in
    prose without tripping it. A comment is not an ast.Constant at all; a docstring
    is, so it is subtracted explicitly.
    """
    tree = ast.parse(path.read_text())
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
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    if name.endswith("collector"):
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


def test_every_copy_exposes_the_whole_contract():
    """Byte-identity already implies this. It is asserted by NAME anyway, because a
    consumer importing a fragment that a sibling copy does not export fails at
    runtime in one Lambda and nowhere in this suite."""
    contract = ("SCOPED_ROWS", "ALL_ROWS", "LATEST_SCOPED_TIME_SUBQUERY",
                "ESTABLISHED_SCOPE_SQL", "OBSERVED_SQL", "COVERAGE_SQL",
                "CONFIRM_WITHIN_SEC", "OBSERVATION_STATUSES", "DROPPED_CAVEAT",
                "compare", "observed", "observation_is_complete", "not_seen_note",
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
