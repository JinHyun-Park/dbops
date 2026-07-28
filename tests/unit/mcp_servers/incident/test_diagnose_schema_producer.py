"""diagnose_root_cause: "no DDL change in the window" vs "no DDL data at all".

`signals_examined` is pre-seeded to 0 for every source, so `skipped_sources` is
the ONLY thing that distinguishes those two. A missing schema_snapshots TABLE was
always reported as skipped; an EMPTY one was not, and once the table exists (E-4
creates it) that gap becomes the live case for every cluster whose family has no
snapshot producer.

That matters more here than for other signals: schema_change carries the highest
base weight in BASE_WEIGHTS, so a silently-absent producer does not merely lose a
signal, it systematically under-ranks the most common real cause of a regression.
"""

from unittest.mock import MagicMock

from mcp_servers.incident.tools.diagnose_root_cause import (
    BASE_WEIGHTS,
    _collect_schema_changes,
)
from mcp_servers.shared.models import QueryResult

ANCHOR_START = "2026-07-01T11:00:00+00:00"
ANCHOR_END = "2026-07-01T13:00:00+00:00"

_EMPTY = QueryResult(columns=[], rows=[], row_count=0)


def _probe(snapshots, schemas):
    return QueryResult(
        columns=["snapshots", "schemas"],
        rows=[{"snapshots": snapshots, "schemas": schemas}],
        row_count=1,
    )


_SCOPE = "dbops/16384"


def _observation(schemas=(("public", "y", "y"),), scope=_SCOPE,
                 last="2026-07-01T12:00:00Z"):
    """OBSERVED_SQL's shape. `schemas` entries are (name, holds_tables, confirmed);
    "n" ages that schema's OWN last_seen_at past the bar, which is what makes it an
    unconfirmed schema."""
    return QueryResult(
        columns=["schema_name", "read_scope", "last_seen", "holds_tables", "age_sec"],
        rows=[{"schema_name": n, "read_scope": scope, "last_seen": last,
               "holds_tables": h, "age_sec": 60 if c == "y" else 40 * 24 * 3600}
              for n, h, c in schemas],
        row_count=len(schemas),
    )


def _cache(window=None, probe=None, observation=None, scope=_SCOPE,
           window_raises=None, probe_raises=None, obs_raises=None,
           engine="aurora-postgresql"):
    """A cache that DISPATCHES ON SQL rather than on call order: the source now
    issues the shared observation probe as well, and a positional side_effect list
    makes every such change a wall of unrelated red."""
    obs = observation if observation is not None else _observation()

    def execute(sql, params=None):
        # THE DIALECT, resolved per cluster from cluster_meta.engine, and asked FIRST:
        # schema snapshots are PostgreSQL-only (MySQL's information_schema is
        # privilege-filtered, so a REVOKE and a DROP are the same read).
        if "FROM cluster_meta" in sql:
            return QueryResult(columns=["engine"],
                               rows=[] if engine is None else [{"engine": engine}],
                               row_count=0 if engine is None else 1)
        if "read_scope IS NOT NULL" in sql:
            if obs_raises:
                raise obs_raises
            if scope is None:
                return QueryResult(columns=["read_scope"], rows=[], row_count=0)
            return QueryResult(columns=["read_scope"], rows=[{"read_scope": scope}],
                               row_count=1)
        if "holds_tables" in sql:
            if obs_raises:
                raise obs_raises
            return obs
        if "COUNT(*) AS snapshots" in sql:
            if probe_raises:
                raise probe_raises
            return probe if probe is not None else _probe(0, 0)
        if window_raises:
            raise window_raises
        return window if window is not None else _EMPTY
    cache = MagicMock()
    cache.execute.side_effect = execute
    return cache


def _run(**kwargs):
    cache = _cache(**kwargs)
    examined, skipped = {}, []
    out = _collect_schema_changes(cache, "prod-pg-1", ANCHOR_START, ANCHOR_END,
                                  None, 60, examined, skipped)
    return out, examined, skipped, cache


def test_schema_change_is_the_highest_weighted_category():
    """Context for why the producer probe matters at all."""
    assert BASE_WEIGHTS["schema_change"] == max(BASE_WEIGHTS.values())


def test_no_snapshots_at_all_is_skipped_not_examined_zero():
    out, examined, skipped, _ = _run(probe=_probe(0, 0),
                                     observation=_EMPTY, scope=None)
    assert out == []
    assert skipped == ["schema_changes"]
    assert "schema_changes" not in examined


def test_baseline_only_is_skipped_because_no_diff_could_exist():
    """One snapshot per schema cannot produce a diff row, so an empty window
    proves nothing about DDL."""
    out, examined, skipped, _ = _run(probe=_probe(3, 3))
    assert out == []
    assert skipped == ["schema_changes"]


def test_comparable_history_with_an_empty_window_is_a_real_negative():
    """Snapshots exist and at least one schema has two, so "no DDL change in this
    window" is supportable: examined, NOT skipped."""
    out, examined, skipped, _ = _run(probe=_probe(5, 2))
    assert out == []
    assert skipped == []
    assert examined["schema_changes"] == 0


def test_rows_in_the_window_skip_the_probe_entirely():
    out, examined, skipped, cache = _run(window=QueryResult(
        columns=["snapshot_time", "schema_name", "changes"],
        rows=[{"snapshot_time": "2026-07-01T12:00:00+00:00", "schema_name": "app",
               "changes": '{"dropped": ["orders"]}'}],
        row_count=1,
    ))
    assert skipped == []
    assert examined["schema_changes"] == 1
    assert out[0]["category"] == "schema_change"
    assert out[0]["evidence"]["schema_name"] == "app"
    # The window read, the DIALECT lookup and the two shared observation statements,
    # and NO producer probe: there is nothing to qualify when rows came back.
    assert cache.execute.call_count == 4
    assert not any("COUNT(*) AS snapshots" in c.args[0]
                   for c in cache.execute.call_args_list)


def test_probe_failure_is_labelled_apart_from_a_healthy_no_history_skip():
    """A probe that RAISED and a probe that ran and said "no history" are both
    "we did not look", but only one of them is normal. Sharing the single label
    `schema_changes` is what let a column typo inside the probe survive a green
    suite: every assertion on this path passed either way."""
    out, examined, skipped, _ = _run(probe_raises=RuntimeError("cache unavailable"))
    assert out == []
    assert skipped == ["schema_changes_probe_error"]
    assert "schema_changes" not in examined

    # And the healthy no-history skip must NOT borrow that label.
    _, _, healthy_skipped, _ = _run(probe=_probe(3, 3))
    assert healthy_skipped == ["schema_changes"]


def test_the_window_read_failing_is_labelled_apart_from_no_history():
    """The MAIN schema_snapshots read (a cache DB without schema_v26, no
    permission, cache down) used to append the bare `schema_changes` label, which
    is byte-identical to the healthy "this cluster has no comparable history"
    skip. The previous pass fixed exactly this conflation on the PROBE read 12
    lines below and left it in place here."""
    out, examined, skipped, cache = _run(
        window_raises=RuntimeError('relation "schema_snapshots" does not exist'))
    assert out == []
    assert skipped == ["schema_changes_read_error"]
    assert "schema_changes" not in examined
    # Neither the probe NOR the observation runs: the source returns immediately,
    # because there is nothing to qualify.
    assert cache.execute.call_count == 1

    _, _, no_history, _ = _run(probe=_probe(3, 3))
    assert no_history == ["schema_changes"]
    assert skipped != no_history, (
        "a cache DB with no schema_snapshots table must not read as a cluster "
        "with no DDL history"
    )


def test_all_nine_states_of_this_source_are_distinguishable():
    """The enumeration, driven. signals_examined is pre-seeded to 0 for every
    source, so on the skipped paths the LABEL is the only difference, and two states
    sharing one label is the defect this tier keeps relocating.

    A SIXTH state joined them in the sixth pass, and it is the accepted cost of the
    whole surface: a schema nobody can currently confirm files no diff row, so an
    empty window is not evidence that no DDL happened. Under the fifth pass that
    state was byte-identical to "comparable history, empty window", i.e. to a real
    negative, in the HIGHEST-weighted source.

    A NINTH joins them now, and it is a REFUSAL rather than a failure: this cluster's
    engine has a privilege-filtered catalog (measured on MySQL 9.3.0: a REVOKE and a
    DROP produce the identical read, and CURRENT_USER() does not move between them),
    so nothing is collected for it and this source contributes no candidate at all.
    Without its own label it would have been byte-identical to "no comparable
    history", i.e. to a young PostgreSQL cluster, which is the conflation this
    enumeration exists to prevent.
    """
    rows = QueryResult(
        columns=["snapshot_time", "schema_name", "changes"],
        rows=[{"snapshot_time": "2026-07-01T12:00:00+00:00", "schema_name": "app",
               "changes": '{"dropped": ["orders"]}'}],
        row_count=1,
    )
    unconfirmed = _observation((("app", "y", "y"), ("gone", "y", "n")))

    states = {}
    for label, cache in (
        ("window read raised",
         _cache(window_raises=RuntimeError("no such table"))),
        ("probe raised", _cache(probe_raises=RuntimeError("cache gone"))),
        ("observation unavailable",
         _cache(probe=_probe(5, 2), obs_raises=RuntimeError("no schema_v27"))),
        ("nothing comparable, every row predates schema_v27",
         _cache(probe=_probe(5, 2), scope=None,
                observation=_observation((("app", "y", "y"),), scope=None))),
        ("no comparable history", _cache(probe=_probe(3, 3))),
        ("comparable history, empty window", _cache(probe=_probe(5, 2))),
        ("comparable history, empty window, a schema unconfirmed",
         _cache(probe=_probe(5, 2), observation=unconfirmed)),
        ("rows in the window", _cache(window=rows)),
        # THE REFUSAL. Note the rows: even WITH stored history in the window, this
        # source contributes nothing, because a schema_change candidate is the
        # highest-weight thing this tool can rank and on this dialect it might be a
        # permission change.
        ("engine whose catalog cannot support the claim",
         _cache(window=rows, engine="aurora-mysql")),
    ):
        examined, skipped = {}, []
        got = _collect_schema_changes(cache, "prod-pg-1", ANCHOR_START, ANCHOR_END,
                                     None, 60, examined, skipped)
        sig = (tuple(skipped), examined.get("schema_changes", "absent"), len(got))
        assert sig not in states, f"{label} is indistinguishable from {states.get(sig)}"
        states[sig] = label
    assert len(states) == 9
    # EXACTLY TWO states carry no label: the state with rows (which needs none, the
    # rows are the answer) and the one real negative. Every other state is labelled,
    # so "examined 0 and nothing skipped" means "we looked and there was no DDL
    # change" and can mean nothing else.
    assert sorted(lbl for sig, lbl in states.items() if sig[0] == ()) == [
        "comparable history, empty window", "rows in the window"]
    unlabelled_negatives = [lbl for sig, lbl in states.items()
                            if sig[0] == () and sig[2] == 0]
    assert unlabelled_negatives == ["comparable history, empty window"]


def test_an_unconfirmed_schema_is_reported_even_when_another_schema_changed():
    """The unknown is not confined to the empty branch: a schema nobody can confirm
    is silent here whether or not some other schema changed in the window, so the
    label rides along with the rows."""
    rows = QueryResult(
        columns=["snapshot_time", "schema_name", "changes"],
        rows=[{"snapshot_time": "2026-07-01T12:00:00+00:00", "schema_name": "app",
               "changes": '{"dropped": ["orders"]}'}],
        row_count=1,
    )
    out, examined, skipped, _ = _run(
        window=rows,
        observation=_observation((("app", "y", "y"), ("gone", "y", "n"))))
    assert examined["schema_changes"] == 1
    assert len(out) == 1
    assert skipped == ["schema_changes_unconfirmed_schemas"]


def test_a_cluster_with_no_snapshots_at_all_is_not_labelled_unconfirmed():
    """`no_snapshots` is not an unknown about a schema, it is the absence of any
    schema, and it already has its own label. Two labels for one state is the
    conflation in reverse."""
    _, _, skipped, _ = _run(probe=_probe(0, 0), observation=_EMPTY, scope=None)
    assert skipped == ["schema_changes"]


def test_probe_sql_is_a_module_constant_so_a_test_can_execute_it():
    """It was an inline string, and it was the one statement in this tier that no
    test ever ran. test_schema_snapshot_real_pg.py executes this constant against
    a real server."""
    from mcp_servers.incident.tools.diagnose_root_cause import SCHEMA_PRODUCER_PROBE_SQL

    assert "COUNT(DISTINCT schema_name) AS schemas" in SCHEMA_PRODUCER_PROBE_SQL
    assert "COUNT(*) AS snapshots" in SCHEMA_PRODUCER_PROBE_SQL
    assert ":cluster_id" in SCHEMA_PRODUCER_PROBE_SQL
