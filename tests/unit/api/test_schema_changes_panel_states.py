"""THE STATE MATRIX for the schema-changes surface, end to end to the sentence.

This surface has FOUR independent signals and three review passes each fixed one
cell and broke or missed another:

  pass 1  the dashboard's `dropped` branch was unreachable, so a real DROP was
          never reported at all.
  pass 2  fixed, and the stale-cluster case became a self-comparison reported as
          `ok`.
  pass 3  fixed, and the vanished-schema guard was made unreachable by another
          change in the same commit. Two further findings were "the fix stops at
          the API boundary and the operator still reads the old sentence".
  pass 4  fixed, and `partial_window` was left out of the blindness test and out
          of the matrix, so a 30-day question answered from 7 days of snapshots
          reached `no_changes`. The enumeration was written to stop exactly that
          and it did not, because it was a LIST of interesting cells and not a
          PRODUCT. It is a product now, and `test_every_blindness_list_in_the_
          payload_forbids_no_changes` derives the guard from the payload's own
          shape so a SIXTH blindness list needs no test edit to be covered.

So this module enumerates every cell and asserts each one is DISTINGUISHABLE all
the way to what a human reads:

  API half     `_schema_changes` is driven for each cell and the four signal
               values plus the top-level `status` are asserted. The `query`
               callable is a fake here ON PURPOSE: the matrix is a cross-product
               of signal values, and a real engine cannot be coerced into
               `ddl=unavailable` or `ok-with-one-blind-schema` without 12
               separate fixtures. The fake NEVER decides a signal: it returns
               snapshot/table_stats/freshness ROWS and the shipped derivation
               computes every status under test.
               The other half of that split is
               tests/unit/api/test_dashboard_schema_changes_real_pg.py, where the
               same cells are produced by the SHIPPED SQL on a real PostgreSQL
               from real DDL. Neither test is sufficient alone: this one would
               pass against SQL that cannot parse, that one cannot enumerate.

  PANEL half   BOTH branch chains of
               frontend/src/components/dashboard/schema-changes-panel.tsx are
               PARSED and MODELLED, so the assertion is about the JSX something
               REACHES: `EmptyVerdict` for an empty list (`panel_verdict`) and
               `ChangeRow`'s per-type cells for a change row
               (`panel_change_row`). The previous round modelled only the empty
               half, which excluded `status: ok` by construction and left the
               branch that renders the changes themselves covered by nothing.
               An earlier round guarded this with `assert field in src`, which
               cannot tell a rendered branch from a type-map key or a comment,
               and it also claimed a payload/panel contract test that did not
               exist. Same idiom as
               tests/unit/test_anomalies_panel_empty_state.py (branch chain),
               test_capacity_panel_family_table.py (a table out of tsx) and
               test_metric_filters.py (SQL predicates); there is no JS runtime in
               CI.

MUTATION-CHECKED. Every guard here was broken, the failure OBSERVED, and the file
restored from a backup. Counts are what pytest reported:
  1. delete the whole `if (d.status === "partial")` branch
       -> 9 failed, incl. test_each_api_status_reaches_its_own_sentence (partial
          fell through to the deploy-skew branch) and
          test_the_five_statuses_read_differently_from_each_other.
  2. remove `{measured}` from the no_changes branch
       -> 1 failed: test_the_two_counting_states_show_what_was_compared.
  3. swap `<EmptyVerdict d={data} />` for an inline unqualified <div>
       -> 1 failed: test_the_verdict_component_is_reached_from_the_render.
  4. revert the handler to `elif ddl_status == "ok" or rows_status == "ok"`
       -> 17 failed, incl. test_outside_window_does_not_headline_as_no_changes,
          test_a_blind_schema_beside_a_compared_one_is_not_a_negative and 7 signal
          tuples.
  5. key the baseline_only note back to `ddl_status == "baseline_only"`
       -> 1 failed:
          test_a_baseline_only_schema_beside_a_compared_one_is_named_in_the_note.
  6. delete the `outside_window` entry from DDL_CHIP
       -> 1 failed: test_every_source_status_the_server_can_emit_has_a_chip.
  7. flip that entry to `ok: true` (blindness drawn as normal)
       -> 1 failed: test_exactly_one_chip_entry_per_source_is_marked_ok.
  8. stop rendering `{data.note}`
       -> 1 failed: test_the_note_the_server_wrote_is_rendered.
  9. flip ROWS_CHIP's `ok` entry to `ok: false`
       -> 1 failed: test_exactly_one_chip_entry_per_source_is_marked_ok.
 10. rewrite the partial guard onto another field
     (`d.row_deltas?.status === "ok"`)
       -> _PREDICATES refused it: every parametrized case failed.
 11. smuggle in an unmodelled branch (`if (d.days > 30)`)
       -> _PREDICATES refused it: every parametrized case failed.
 12. add a payload field the panel does not read
       -> 1 failed: test_no_payload_field_reaches_the_operator_by_accident.

FIFTH PASS. Its two findings were "the empty half is modelled and the POSITIVE
half is modelled by NOTHING" and "partial_window appears in no row of the matrix".
Both were REPRODUCED first. Pre-fix, against the shipped panel and the whole
`pytest tests/unit` (baseline 2331 passed):
  * delete the `{c.change_type === "dropped" && ...}` cell      -> 2331 passed
  * replace the list-branch operand with an empty <div>, i.e.
    changes.map AND renames.map deleted                         -> 2331 passed
so pass 1's original defect, a real DROP rendered as nothing at all, was
reintroducible green in both halves.
And on a real PostgreSQL (tests/unit/api/test_dashboard_schema_changes_real_pg.py
harness), 3 snapshots starting 7 days ago asked about days=30, i.e.
baseline_outside_window TRUE with row deltas ok and collection fresh:
  status "no_changes", panel headline "이 구간에서 감지된 변경 없음".

Mutation-checked after the fix. Counts are what pytest reported for this module
plus test_dashboard_schema_changes_real_pg.py on 13/14/19/20 (138 passed clean),
this module alone on the rest (103 passed clean):
 13. delete the dropped-row cell from ChangeRow
       -> 7 failed, incl. test_a_dropped_row_shows_the_count_it_lost,
          test_every_change_type_the_server_emits_renders_something and
          real_pg::test_real_drop_is_reported_and_the_old_sql_could_not_see_it.
 14. dropped cell reads `value={current}`, which is None for every drop
       -> 3 failed.
 15. delete the unknown-change_type fallthrough
       -> 1 failed: test_an_unknown_change_type_is_marked_unknown_and_not_silent.
 16. list-branch operand replaced with an empty <div>
       -> 1 failed: test_the_list_branch_renders_both_kinds_of_change.
 17. `const shown = changes.length`, so renames stop counting
       -> 1 failed: test_the_list_branch_renders_both_kinds_of_change.
 18. smuggle a ONE-LINE cell into ChangeRow
     (`{c.baseline_rows === 0 && <span>zero</span>}`)
       -> 7 failed. It passed 103 until _row_cells claimed EVERY expression at
          the cells' indentation instead of only lines ending in ` && (`.
 19. handler: `ddl_blind = baseline_only + outside_window`, i.e. forget
     partial_window again
       -> 11 failed, incl. both partial_window matrix cells, product cells
          ok+partial_window__ok_fresh / __ok_stale,
          test_no_changes_appears_exactly_where_the_product_says_it_does and
          test_every_blindness_list_in_the_payload_forbids_no_changes.
 20. handler: drop `ddl_available and` from the not_collected guard
       -> 4 failed, incl. product cell unavailable__no_data_no_data.
 21. delete the `ok+partial_window` constructor from _DDL_CASES: take a signal
     VALUE back out of the product, which is how all four previous passes escaped
       -> 1 failed: test_every_blindness_list_in_the_payload_forbids_no_changes.
 22. handler grows a SIXTH `*_schemas` list, non-empty under `no_changes`
       -> 2 failed with NO test edit: the structural guard plus
          test_no_payload_field_reaches_the_operator_by_accident.

NOT pinned: branch ORDER in the panel (the guards are mutually exclusive equality
tests on one field, so any order is the same function), and the exact Korean
wording beyond the phrases each state must and must not contain.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_DASHBOARD_DIR = _ROOT / "api" / "dashboard"
_PANEL_PATH = _ROOT / "frontend/src/components/dashboard/schema-changes-panel.tsx"
_PANEL = _PANEL_PATH.read_text()

sys.path.insert(0, str(_DASHBOARD_DIR))
os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")
_spec = importlib.util.spec_from_file_location(
    "dashboard_handler_panel_states", _DASHBOARD_DIR / "handler.py")
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)
_HANDLER_SRC = (_DASHBOARD_DIR / "handler.py").read_text()


# ===========================================================================
# The panel's branch chain, parsed
# ===========================================================================

def _flat(s: str) -> str:
    return " ".join(s.split())


def _verdict_body() -> str:
    """The EmptyVerdict component, declaration to the next top-level one."""
    start = _PANEL.index("function EmptyVerdict(")
    return _PANEL[start:_PANEL.index("\nfunction ", start + 1)]


def _changerow_body() -> str:
    """The ChangeRow component, declaration to the next top-level one."""
    start = _PANEL.index("function ChangeRow(")
    return _PANEL[start:_PANEL.index("\nexport function ", start + 1)]


def _list_branch() -> str:
    """The NON-empty operand of the `shown === 0 ? ... : ...` ternary: the JSX that
    renders the changes themselves."""
    s = _PANEL[_PANEL.index("{shown === 0 ? ("):]
    return _flat(s[s.index(") : ("):s.index("\n          )}")])


# Each guard EmptyVerdict may carry, as a predicate over the payload. Parsing
# REFUSES anything not listed, so a new branch cannot appear without a test here
# saying which payload reaches it, and a guard rewritten to test some other field
# fails loudly instead of silently swallowing a state.
_PREDICATES = {
    'd.status === "no_changes"': lambda p: p.get("status") == "no_changes",
    'd.status === "partial"': lambda p: p.get("status") == "partial",
    'd.status === "not_collected"': lambda p: p.get("status") == "not_collected",
    'd.status === "insufficient_history"': lambda p: p.get("status") == "insufficient_history",
}


def _branches():
    """[(guard_source, predicate, branch_jsx)] in source order, with the
    fallthrough last carrying a guard of None. Source position models the
    if-chain; it is not itself under test (see the module docstring)."""
    body = _verdict_body()
    guards = list(re.finditer(r"^  if \((.*?)\) \{", body, re.M))
    assert guards, (
        "EmptyVerdict has no branches at all: every status now renders the same "
        "sentence, which is the collapse this whole module exists to prevent"
    )
    out = []
    for g in guards:
        cond = g.group(1)
        assert cond in _PREDICATES, (
            f"unrecognized EmptyVerdict guard {cond!r}. Add it to _PREDICATES and "
            "assert which payload reaches it: an unmodelled branch could be "
            "swallowing a state the panel is supposed to distinguish."
        )
        close = body.index("\n  }", g.end())
        out.append((cond, _PREDICATES[cond], body[g.start():close]))
    tail = body.index("\n  }", guards[-1].end())
    out.append((None, lambda _p: True, body[tail:]))
    return out


# The POSITIVE half, modelled the same way. The previous round modelled only the
# EMPTY half, so the branch that renders the changes THEMSELVES was covered by
# nothing: with `changes.map` and `renames.map` both deleted the suite reported
# 2331 passed, i.e. pass 1's original defect (a real DROP rendered as nothing at
# all) was reintroducible green. One predicate per cell of ChangeRow's row-count
# container, including the unknown-type fallthrough, and parsing REFUSES anything
# not listed here.
_ROW_PREDICATES = {
    'c.change_type === "created"': lambda c: c.get("change_type") == "created",
    'c.change_type === "dropped"': lambda c: c.get("change_type") == "dropped",
    'c.change_type === "changed"': lambda c: c.get("change_type") == "changed",
    "!KNOWN_CHANGE.includes(c.change_type)":
        lambda c: c.get("change_type") not in ("created", "dropped", "changed"),
}


def _row_cells() -> dict:
    """{guard_source: cell_jsx} for ChangeRow's row-count container.

    EVERY expression at the cells' indentation is claimed, not just the ones
    shaped like a cell: matching `\\{(.*?) && \\($` alone let a ONE-LINE
    conditional slip in unmodelled (measured: `{c.baseline_rows === 0 &&
    <span>zero</span>}` added to ChangeRow and the suite still reported 103
    passed), and an unmodelled cell can render a second reading of one event."""
    body = _changerow_body()
    out = {}
    for m in re.finditer(r"^ {10}\{(.+)$", body, re.M):
        line = m.group(1)
        assert line.endswith(" && ("), (
            f"unrecognized expression in ChangeRow's cell container: {{{line}}}. "
            "Cells are `{<guard> && (` on their own line so this model can see "
            "them; anything else could be rendering a change row nothing here "
            "asserts."
        )
        cond = line[:-len(" && (")]
        assert cond in _ROW_PREDICATES, (
            f"unrecognized ChangeRow cell guard {cond!r}. Add it to _ROW_PREDICATES "
            "and assert which change row reaches it."
        )
        out[cond] = _flat(body[m.start():body.index("\n          )}", m.end())])
    assert out, (
        "ChangeRow renders no per-type cell at all: every change row now shows its "
        "name and nothing about what happened to it"
    )
    return out


def panel_change_row(change: dict) -> str:
    """The JSX a CHANGE row reaches, flattened. Exactly one cell must match: zero
    means the row renders as nothing (the pass-1 defect), two means two readings
    of one event.

    Exported for tests/unit/api/test_dashboard_schema_changes_real_pg.py, which
    feeds it rows the SHIPPED derivation produced from real DDL on a real
    engine."""
    hit = [jsx for cond, jsx in _row_cells().items() if _ROW_PREDICATES[cond](change)]
    assert len(hit) == 1, (
        f"change_type {change.get('change_type')!r} reaches {len(hit)} cells of "
        "ChangeRow, expected exactly 1", sorted(_row_cells())
    )
    return hit[0]


def panel_verdict(payload: dict) -> str:
    """The JSX EmptyVerdict actually reaches for this payload, flattened.

    Exported: tests/unit/api/test_dashboard_schema_changes_real_pg.py feeds it
    payloads the SHIPPED SQL produced on a real engine, which is the link from a
    real DDL event to the sentence an operator reads."""
    for _cond, pred, jsx in _branches():
        if pred(payload):
            return _flat(jsx)
    raise AssertionError("no branch matched: EmptyVerdict has no fallthrough return")


# The one sentence that asserts an absence of change. Reachable from exactly one
# state, and the whole point of the matrix is that nothing else reaches it.
_NEUTRAL = "이 구간에서 감지된 변경 없음"
# The unqualified empty state the previous two commits exist to delete.
_OLD_UNQUALIFIED = "감지된 스키마 변경 없음"


# ===========================================================================
# The API half of the matrix: a fake `query`, the shipped derivation
# ===========================================================================

_T0 = "2026-07-20 00:00:00+00"
_T1 = "2026-07-27 00:00:00+00"


_SCOPE = "dbops/16384"
_CONFIRMED_AT = "2026-07-27 00:00:00+00"


def _snap(schema="app", n=2, before=None, after=None, *, outside=False,
          is_latest=False, stored=None):
    """One row of what _SCHEMA_SNAPSHOT_PAIRS_SQL returns. `tables_*` are JSON
    STRINGS because that is what the Data API's stringValue branch hands back."""
    import json
    return {
        "schema_name": schema,
        "snapshots_for_schema": n,
        "tables_before": json.dumps(before if before is not None else {"users": ["id"]}),
        "tables_after": json.dumps(after if after is not None else {"users": ["id"]}),
        "baseline_time": _T0,
        "baseline_outside_window": outside,
        "baseline_is_latest": is_latest,
        "current_time": _T1,
        "snapshots_stored": stored if stored is not None else n,
        "first_snapshot": _T0,
        "last_snapshot": _T1,
    }


def _obs_row(schema="app", *, holds="y", confirmed=True, scope=_SCOPE):
    """One row of what OBSERVED_SQL returns: the LATEST snapshot of one schema,
    carrying that schema's OWN read_scope and its OWN last_seen_at age.

    `confirmed=False` ages THAT SCHEMA's stamp past the bar. It is per-schema and
    absolute on purpose: the previous shape asked "is this schema the most recently
    seen one in the cluster", so with every schema sharing one stamp (which is what
    the collector writes, one run timestamp per cycle) none was ever unconfirmed
    however old the stamp was."""
    return {"schema_name": schema, "read_scope": scope, "last_seen": _CONFIRMED_AT,
            "holds_tables": holds, "age_sec": 60 if confirmed else 40 * 24 * 3600}


def _stat(table="t", base=1000, cur=1000, *, in_window=True, schema="app"):
    """One row of what _TABLE_STATS_WINDOW_SQL returns."""
    return {
        "schema_name": schema, "table_name": table,
        "baseline_rows": base, "current_rows": cur,
        "baseline_time": _T0, "current_time": _T1,
        "current_in_window": in_window,
    }


def drive(*, snaps=(), stats=(), age_sec=60, snaps_fail=False, days=7,
          obs=None, scope=_SCOPE, obs_fail=False, engine="aurora-postgresql"):
    """Run the SHIPPED `_schema_changes` over these rows.

    The fake dispatches on WHICH statement is being run and returns ROWS. It
    computes no status: `collection`, `ddl_detection.status`, `row_deltas.status`,
    `observation.status` and the top-level `status` are all derived by the code under
    test.

    `obs` defaults to "every schema in `snaps` confirmed under `scope`", so a cell
    that is not about the observation gets a fully confirmed cluster rather than a
    silently blind one. `age_sec=None` means table_stats has no row for this
    cluster.
    """
    default_obs = [_obs_row(r["schema_name"], scope=scope) for r in snaps]
    obs_rows = default_obs if obs is None else list(obs)

    def query(sql, params=None):
        # ORDER MATTERS: both observation statements name schema_snapshots too, so
        # they have to be claimed before the pairs branch.
        #
        # The DIALECT, asked FIRST by the shared probe. Schema snapshots are
        # PostgreSQL-only: MySQL's information_schema is privilege-filtered in every
        # bucket a diff is derived from, so a REVOKE and a DROP are the same read.
        # `engine=None` models a cluster with no cluster_meta row yet, which is
        # `unavailable` (we cannot decide) rather than either answer.
        if "FROM cluster_meta" in sql:
            return [] if engine is None else [{"engine": engine}]
        if "read_scope IS NOT NULL" in sql:
            if obs_fail:
                raise RuntimeError(
                    'column "read_scope" does not exist; secret '
                    "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:cache-AbCdEf")
            return [] if scope is None else [{"read_scope": scope}]
        if "holds_tables" in sql:
            if obs_fail:
                raise RuntimeError("column last_seen_at does not exist")
            return obs_rows
        if "schema_snapshots" in sql:
            if snaps_fail:
                raise RuntimeError(
                    'relation "schema_snapshots" does not exist; secret '
                    "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:cache-AbCdEf")
            return list(snaps)
        if "MAX(snapshot_time)" in sql:
            if age_sec is None:
                return [{"last_collected": None, "age_sec": None}]
            return [{"last_collected": _T1, "age_sec": age_sec}]
        return list(stats)
    return handler._schema_changes(query, "matrix-1", days)


def signals(p: dict) -> tuple:
    """FIVE signals now, not four. `observation.status` is the channel the fifth
    pass added to the two MCP readers and NOT to this panel, which is how a genuine
    DROP SCHEMA reached the operator as "no changes detected": `not_seen` was a
    signal value that appeared in no row of this matrix, so the matrix guard was
    structurally unable to see it."""
    return (p["status"], p["ddl_detection"]["status"], p["row_deltas"]["status"],
            p["collection"]["status"], p["observation"]["status"])


_FRESH, _STALE_SEC, _DEAD = 60, 48 * 3600, None

# ---------------------------------------------------------------------------
# EVERY CELL. (id, kwargs for drive(), expected signals, panel phrase that must
# appear, phrases that must NOT). The four signals are asserted as a tuple so a
# cell cannot pass by accident on one of them.
# ---------------------------------------------------------------------------
_MATRIX = [
    # --- never collected -------------------------------------------------
    ("never_collected",
     dict(age_sec=_DEAD),
     ("not_collected", "not_collected", "no_data", "no_data", "no_snapshots"),
     "수집 이력이 없어", [_NEUTRAL]),

    # --- the pair read raises (a permission, a missing column) ------------
    # `obs=` is supplied on all three: a cache DB where schema_snapshots is
    # READABLE and the pair statement specifically fails. The case where the whole
    # TABLE is unreadable is its own cell further down (`obs_fail`), because the
    # observation probe reads the same relation and cannot succeed while this fails.
    ("pair_read_raises_but_rows_compare",
     dict(snaps_fail=True, obs=[_obs_row()], stats=[_stat(base=1000, cur=1000)]),
     ("partial", "unavailable", "ok", "fresh", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("pair_read_raises_and_no_rows_either",
     dict(snaps_fail=True, obs=[_obs_row()], stats=[_stat(base=None, cur=5)]),
     ("insufficient_history", "unavailable", "insufficient_history", "fresh", "fresh"),
     "비교 가능한 이력이 부족해", [_NEUTRAL]),
    # A FAILED read leaves snapshots_stored at 0 for want of a read, not because
    # the table is empty, so this may not headline "not_collected": that status
    # and _SC_NO_HISTORY both assert both sources hold nothing for this cluster.
    ("pair_read_raises_and_nothing_in_table_stats_either",
     dict(snaps_fail=True, obs=[_obs_row()], age_sec=_DEAD),
     ("insufficient_history", "unavailable", "no_data", "no_data", "fresh"),
     "비교 가능한 이력이 부족해", [_NEUTRAL]),

    # --- history STARTS inside the window (FINDING 2's cell) --------------
    # 30 days asked, 7 days of snapshots. The pair is real and the diff is
    # empty, but only over the span that existed, so this is not a negative for
    # the 30 days. It reached `no_changes` until `partial_window` was added to
    # the blindness test.
    ("partial_window_history_shorter_than_the_window",
     dict(snaps=[_snap(outside=True)], stats=[_stat(base=1000, cur=1000)], days=30),
     ("partial", "ok", "ok", "fresh", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("one_schema_compared_one_partial_window",
     dict(snaps=[_snap(schema="full_s", stored=4),
                 _snap(schema="short_s", outside=True, stored=4)],
          stats=[_stat(base=1000, cur=1000)], days=30),
     ("partial", "ok", "ok", "fresh", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),

    # --- snapshots entirely outside the window (FINDING 3's cell) --------
    ("outside_window_but_rows_compare",
     dict(snaps=[_snap(is_latest=True)], stats=[_stat(base=1000, cur=1000)]),
     ("partial", "outside_window", "ok", "fresh", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("outside_window_and_rows_too_old",
     dict(snaps=[_snap(is_latest=True)], stats=[_stat(in_window=False)],
          age_sec=40 * 24 * 3600),
     ("insufficient_history", "outside_window", "insufficient_history", "stale", "fresh"),
     "비교 가능한 이력이 부족해", [_NEUTRAL]),

    # --- a blind schema BESIDE a compared one ----------------------------
    ("one_schema_compared_one_outside_window",
     dict(snaps=[_snap(schema="ok_s", after={"users": ["id"]}, stored=4),
                 _snap(schema="blind_s", is_latest=True, stored=4)],
          stats=[_stat(base=1000, cur=1000)]),
     ("partial", "ok", "ok", "fresh", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("one_schema_compared_one_baseline_only",
     dict(snaps=[_snap(schema="ok_s", stored=3),
                 _snap(schema="new_s", n=1, stored=3)],
          stats=[_stat(base=1000, cur=1000)]),
     ("partial", "ok", "ok", "fresh", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),

    # --- one source silent -----------------------------------------------
    ("ddl_never_collected_rows_ok",
     dict(stats=[_stat(base=1000, cur=1000)]),
     ("partial", "not_collected", "ok", "fresh", "no_snapshots"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("ddl_baseline_only_rows_ok",
     dict(snaps=[_snap(n=1, stored=1)], stats=[_stat(base=1000, cur=1000)]),
     ("partial", "baseline_only", "ok", "fresh", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("ddl_ok_rows_have_one_endpoint",
     dict(snaps=[_snap()], stats=[_stat(base=None, cur=7)]),
     ("partial", "ok", "insufficient_history", "fresh", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),

    # --- the only cell that may read as an absence of change -------------
    ("fresh_and_both_sources_compared",
     dict(snaps=[_snap()], stats=[_stat(base=1000, cur=1000)]),
     ("no_changes", "ok", "ok", "fresh", "fresh"),
     _NEUTRAL, ["일부 신호만 판정됨", "판정할 수 없음"]),
    ("stale_but_still_inside_the_window",
     dict(snaps=[_snap()], stats=[_stat(base=1000, cur=1000)], age_sec=_STALE_SEC),
     ("no_changes", "ok", "ok", "stale", "fresh"),
     _NEUTRAL, ["일부 신호만 판정됨"]),

    # --- real changes ----------------------------------------------------
    ("stale_with_a_real_ddl_change",
     dict(snaps=[_snap(before={"users": ["id"]},
                       after={"users": ["id"], "created_tbl": ["id"]})],
          stats=[_stat(base=1000, cur=1000)], age_sec=_STALE_SEC),
     ("ok", "ok", "ok", "stale", "fresh"), None, None),
    ("fresh_with_a_real_ddl_change",
     dict(snaps=[_snap(before={"users": ["id"], "gone_tbl": ["id"]},
                       after={"users": ["id"]})],
          stats=[_stat(base=1000, cur=1000)]),
     ("ok", "ok", "ok", "fresh", "fresh"), None, None),
    # --- THE ACCEPTED COST, at the point the operator reads it ------------
    # A genuine DROP SCHEMA is never drawn as a drop, because absence cannot be told
    # apart from a read that could not reach the schema. It surfaces as "last
    # confirmed at T, not seen since", and it must never be `no_changes`. MEASURED
    # before this pass on PostgreSQL 14.18: `DROP SCHEMA core CASCADE` gave the
    # collector {"not_seen": 1, "not_seen_schemas": ["core"]} and this function
    # status "no_changes", with the string "core" nowhere in the payload.
    ("a_dropped_schema_is_not_seen_never_no_changes",
     dict(snaps=[_snap(schema="live_s", stored=4)],
          obs=[_obs_row("live_s"), _obs_row("gone_s", confirmed=False)],
          stats=[_stat(base=1000, cur=1000)]),
     ("partial", "ok", "ok", "fresh", "not_seen"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    # An EMPTY schema that stopped being seen is NOT a blindness: nobody is being
    # shown stale contents for it, so it must not downgrade the negative.
    ("an_emptied_schema_that_vanished_is_not_a_blindness",
     dict(snaps=[_snap(schema="live_s")],
          obs=[_obs_row("live_s"), _obs_row("empty_s", holds="n", confirmed=False)],
          stats=[_stat(base=1000, cur=1000)]),
     ("no_changes", "ok", "ok", "fresh", "fresh"),
     _NEUTRAL, ["일부 신호만 판정됨"]),
    # --- history exists and NONE of it is comparable ----------------------
    ("every_row_predates_the_scope_column",
     dict(snaps=[_snap()], scope=None, obs=[_obs_row("app", scope=None)],
          stats=[_stat(base=1000, cur=1000)]),
     ("partial", "not_comparable", "ok", "fresh", "unmigrated"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("the_observation_probe_itself_is_unreadable",
     dict(snaps=[_snap()], obs_fail=True, stats=[_stat(base=1000, cur=1000)]),
     ("partial", "unavailable", "ok", "fresh", "unavailable"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    # --- the engine's catalog cannot support the claim (FINDING 4) ---------
    # MySQL: measured on 9.3.0, a table-level REVOKE removes the table from the read
    # exactly as a DROP does and CURRENT_USER() does not change, so nothing is
    # collected and the panel must not draw a created/dropped chip for this cluster.
    ("a_refused_dialect_is_partial_and_never_no_changes",
     dict(snaps=[], engine="aurora-mysql", stats=[_stat(base=1000, cur=1000)]),
     ("partial", "not_supported", "ok", "fresh", "unsupported_engine"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    # An engine nobody could resolve is a THIRD state: no cluster_meta row yet.
    ("an_unresolvable_engine_is_not_a_refusal",
     dict(snaps=[_snap()], engine=None, stats=[_stat(base=1000, cur=1000)]),
     ("partial", "unavailable", "ok", "fresh", "unavailable"),
     "일부 신호만 판정됨", [_NEUTRAL]),
]


@pytest.mark.parametrize("cell", _MATRIX, ids=[c[0] for c in _MATRIX])
def test_every_cell_of_the_matrix_produces_its_own_signal_tuple(cell):
    _id, kwargs, expected, _phrase, _forbidden = cell
    got = drive(**kwargs)
    assert signals(got) == expected, got


@pytest.mark.parametrize("cell", [c for c in _MATRIX if c[3]], ids=[c[0] for c in _MATRIX if c[3]])
def test_each_api_status_reaches_its_own_sentence(cell):
    """The end of the chain: the status the shipped derivation produced, fed
    through the panel's parsed branch chain, and the sentence an operator reads."""
    _id, kwargs, expected, phrase, forbidden = cell
    got = drive(**kwargs)
    jsx = panel_verdict(got)
    assert phrase in jsx, (f"status={got['status']} does not render {phrase!r}", jsx)
    for bad in forbidden or ():
        assert bad not in jsx, (f"status={got['status']} renders {bad!r}", jsx)


def test_a_change_is_never_rendered_by_the_empty_verdict_at_all():
    """The two `ok` cells put rows in `changes`, so the panel takes the LIST
    branch and EmptyVerdict is not reached. Asserted here rather than left
    implicit, because an `ok` payload reaching the verdict chain would land on the
    deploy-skew fallthrough and read as undeterminable."""
    for cid, kwargs, expected, _p, _f in _MATRIX:
        got = drive(**kwargs)
        if expected[0] == "ok":
            assert got["changes"] or got["ddl_detection"]["rename_candidates"], (cid, got)
        else:
            assert not got["changes"], (cid, got["changes"])
            assert not got["ddl_detection"]["rename_candidates"], cid


# ===========================================================================
# THE PRODUCT. Every value of every signal, crossed.
# ===========================================================================
# The hand-written cells above are the interesting states. They are not a
# PRODUCT, and every pass over this surface escaped through a signal value that
# appeared in no cell: pass 5 through `partial_window`, which was in the payload
# and in the note and in NO row of the matrix, and therefore in no row of the
# blindness test either. So the product is built here mechanically, from one
# constructor per signal value, and the expectation is a LITERAL table.
#
# DDL has NINE structural values, not five: `ddl_detection.status == "ok"` means "at
# least one schema compared", so it splits by WHICH schemas went unanswered.
_DDL_CASES = {
    # complete: every schema, over the whole window
    "ok": dict(snaps=[_snap()]),
    # ok, and blind for one schema, ONE CASE PER BLINDNESS LIST IN THE PAYLOAD
    "ok+baseline_only": dict(snaps=[_snap(schema="ok_s", stored=3),
                                    _snap(schema="one_snap_s", n=1, stored=3)]),
    "ok+partial_window": dict(snaps=[_snap(outside=True)], days=30),
    "ok+outside_window": dict(snaps=[_snap(schema="ok_s", stored=4),
                                     _snap(schema="ancient_s", is_latest=True,
                                           stored=4)]),
    # nothing compared at all, one row per ddl_detection.status
    "not_collected": dict(snaps=[]),
    # history EXISTS and none of it is comparable: every row predates schema_v27, so
    # no row says which catalog it describes.
    "not_comparable": dict(snaps=[_snap()], scope=None,
                           obs=[_obs_row("app", scope=None)]),
    "baseline_only": dict(snaps=[_snap(n=1, stored=1)]),
    "outside_window": dict(snaps=[_snap(is_latest=True)]),
    # TWO ways to reach ddl=unavailable, and they differ in the OBSERVATION column,
    # so they are two cells. Splitting them is not bookkeeping: the exhaustiveness
    # guard below refused the product until `unavailable` appeared in it, which is
    # the guard doing to this pass what it should have done to the previous one.
    "unavailable+pair_read": dict(snaps_fail=True, obs=[_obs_row()]),
    "unavailable+table_unreadable": dict(snaps_fail=True, obs_fail=True),
    # THE REFUSAL, added by the seventh pass. This cluster's engine has a
    # privilege-filtered catalog, so a REVOKE and a DROP are the same read (measured
    # on MySQL 9.3.0) and nothing is collected: no pair is selected AT ALL. Its own
    # status because `not_collected`'s sentence promises a first baseline on the next
    # ETL cycle that is never coming, which is an empty success.
    "not_supported": dict(snaps=[], engine="aurora-mysql"),
}


def _also_not_seen(kw):
    """The same DDL case with ONE unconfirmed table-holding schema beside it.

    `drive()` defaults the observation to "every schema in `snaps` confirmed", so the
    transform has to rebuild that default and append, or it would silently drop the
    case's own schemas out of the observation.
    """
    scope = kw.get("scope", _SCOPE)
    base = list(kw.get("obs") or [_obs_row(r["schema_name"], scope=scope)
                                  for r in kw.get("snaps", ())])
    return {**kw, "obs": base + [_obs_row("gone_s", confirmed=False)]}


# WHICH (ddl, observation) PAIRS A HUMAN CAN REACH, which is FINDING 6 of the seventh
# pass. The previous shape was `_DDL_OBSERVATION`, one observation value pinned per
# ddl case on the claim that the axis was forced. Four of the six values ARE forced,
# because they are the same condition seen from two sides: `no_snapshots` means the
# probe returned no row, which is also why nothing compared; `unmigrated` means no row
# carries a scope, which is why the pair is not comparable; a raising probe forces
# `unavailable`; and a refused dialect forces `not_supported`. The other two,
# `fresh` and `not_seen`, are FREE over every scope-bearing ddl case, and the previous
# shape expressed the one free combination it happened to need ("ok+not_seen") as a
# ninth ddl case, which left (baseline_only, not_seen), (outside_window, not_seen) and
# the rest simply absent from the product while the comment claimed they were
# unreachable. So the two axes are crossed here, and the pairing is the claim:
#   * anything listed is DRIVEN and its observation value ASSERTED, so a pair that
#     turns out unreachable fails rather than sitting unnoticed;
#   * `test_every_observation_status_appears_in_the_product` reads the value set off
#     the SHARED contract, so a new observation value fails here with no edit.
_OBS_TRANSFORMS = {"not_seen": _also_not_seen}
_REACHABLE = {
    # the seven scope-bearing cases: the observation axis is FREE over them
    "ok": ("fresh", "not_seen"),
    "ok+baseline_only": ("fresh", "not_seen"),
    "ok+partial_window": ("fresh", "not_seen"),
    "ok+outside_window": ("fresh", "not_seen"),
    "baseline_only": ("fresh", "not_seen"),
    "outside_window": ("fresh", "not_seen"),
    "unavailable+pair_read": ("fresh", "not_seen"),
    # ...and the four where the observation value and the ddl value are one condition
    "not_collected": ("no_snapshots",),
    "not_comparable": ("unmigrated",),
    "unavailable+table_unreadable": ("unavailable",),
    "not_supported": ("unsupported_engine",),
}


def _cell_kwargs(ddl, obs):
    kw = _DDL_CASES[ddl]
    transform = _OBS_TRANSFORMS.get(obs)
    return transform(kw) if transform else kw

# rows and collection are NOT independent: `no_data` on either side means
# table_stats holds no row for this cluster at all, so rows=no_data <->
# collection=no_data and the 3 x 3 is really these FIVE pairs. Asserted below
# rather than asserted here, so the claim is measured and not just written down.
_ROW_CASES = {
    ("ok", "fresh"): dict(stats=[_stat(base=1000, cur=1000)], age_sec=_FRESH),
    ("ok", "stale"): dict(stats=[_stat(base=1000, cur=1000)], age_sec=_STALE_SEC),
    ("insufficient_history", "fresh"): dict(stats=[_stat(base=None, cur=7)],
                                            age_sec=_FRESH),
    ("insufficient_history", "stale"): dict(stats=[_stat(in_window=False)],
                                            age_sec=_STALE_SEC),
    ("no_data", "no_data"): dict(stats=[], age_sec=_DEAD),
}

# THE EXPECTED TOP-LEVEL STATUS FOR ALL 18 x 5 = 90 CELLS, written out rather than
# recomputed from the handler's rules: a test that re-derives the derivation passes
# for exactly the reasons the derivation is wrong.
#   `no_changes` appears TWICE in this whole table, and only on the row where DDL
#   answered for every schema over the whole window AND every schema was confirmed.
_PARTIAL_5 = ["partial", "partial", "partial", "partial", "partial"]
_INSUF_3 = ["partial", "partial", "insufficient_history", "insufficient_history",
            "insufficient_history"]
_PRODUCT = {
    #                          (ok,fresh)    (ok,stale)    (insuf,fresh)          (insuf,stale)          (no_data,no_data)
    ("ok", "fresh"):        ["no_changes", "no_changes", "partial",              "partial",              "partial"],
    ("ok", "not_seen"):     _PARTIAL_5,
    ("ok+baseline_only", "fresh"): _PARTIAL_5,
    ("ok+baseline_only", "not_seen"): _PARTIAL_5,
    ("ok+partial_window", "fresh"): _PARTIAL_5,
    ("ok+partial_window", "not_seen"): _PARTIAL_5,
    ("ok+outside_window", "fresh"): _PARTIAL_5,
    ("ok+outside_window", "not_seen"): _PARTIAL_5,
    ("not_collected", "no_snapshots"):
        ["partial", "partial", "insufficient_history", "insufficient_history",
         "not_collected"],
    ("not_comparable", "unmigrated"): _INSUF_3,
    ("baseline_only", "fresh"): _INSUF_3,
    ("baseline_only", "not_seen"): _INSUF_3,
    ("outside_window", "fresh"): _INSUF_3,
    ("outside_window", "not_seen"): _INSUF_3,
    ("unavailable+pair_read", "fresh"): _INSUF_3,
    ("unavailable+pair_read", "not_seen"): _INSUF_3,
    ("unavailable+table_unreadable", "unavailable"): _INSUF_3,
    # THE REFUSAL. Same shape as the other "nothing compared" rows: for the operator
    # a refusal IS a blindness, and the only thing that must not happen is
    # `not_collected`, whose sentence promises a baseline that is never coming.
    ("not_supported", "unsupported_engine"): _INSUF_3,
}

_PRODUCT_CELLS = [(d, o, r, i) for d, obs in _REACHABLE.items() for o in obs
                  for i, r in enumerate(_ROW_CASES)]


def _all_cells():
    for d, obs in _REACHABLE.items():
        for o in obs:
            for r in _ROW_CASES:
                yield d, o, r, drive(**{**_cell_kwargs(d, o), **_ROW_CASES[r]})


@pytest.mark.parametrize("ddl,obs,rows,i", _PRODUCT_CELLS,
                         ids=[f"{d}__{o}__{r[0]}_{r[1]}" for d, o, r, _ in _PRODUCT_CELLS])
def test_the_whole_product_of_the_five_signals(ddl, obs, rows, i):
    """Every combination a human can reach, and what the operator reads there."""
    got = drive(**{**_cell_kwargs(ddl, obs), **_ROW_CASES[rows]})
    expected_status = _PRODUCT[(ddl, obs)][i]
    assert signals(got) == (expected_status, ddl.split("+")[0], rows[0], rows[1],
                            obs), got
    # The property the whole surface exists for, asserted on every cell rather
    # than on the cells someone remembered to list.
    assert (_NEUTRAL in panel_verdict(got)) == (expected_status == "no_changes"), (
        ddl, obs, rows, got["status"])


def test_the_product_table_covers_exactly_the_reachable_pairs():
    """The two tables cannot drift apart: a pair added to _REACHABLE with no expected
    row, or an expected row for a pair nobody drives, both fail here."""
    assert set(_PRODUCT) == {(d, o) for d, obs in _REACHABLE.items() for o in obs}


def test_no_changes_appears_exactly_where_the_product_says_it_does():
    """Two cells out of ninety. If a change to the derivation widens that, this
    fails with the cells it added."""
    licensed = {(d, o, r) for (d, o), row in _PRODUCT.items()
                for i, r in enumerate(_ROW_CASES) if row[i] == "no_changes"}
    assert licensed == {("ok", "fresh", ("ok", "fresh")),
                        ("ok", "fresh", ("ok", "stale"))}
    got = {(d, o, r) for d, o, r, payload in _all_cells()
           if payload["status"] == "no_changes"}
    assert got == licensed, sorted(got ^ licensed)


def test_the_two_no_data_signals_are_the_same_signal():
    """Why the row axis is 5 and not 9, measured instead of asserted in a comment:
    rows=no_data and collection=no_data are one condition (table_stats holds no row
    for this cluster), so neither can occur without the other."""
    for d, o, r, got in _all_cells():
        assert (got["row_deltas"]["status"] == "no_data") == \
               (got["collection"]["status"] == "no_data"), (d, o, r, got)


def test_every_blindness_list_in_the_payload_forbids_no_changes():
    """THE ANTI-RELOCATION GUARD, and the reason this round did not add a fifth
    named condition. Any `*_schemas` key of ddl_detection is a set of schemas the
    DDL source could not answer for, so a non-empty one may never coexist with the
    status that reads as an absence of change. A SEVENTH blindness list added later
    is covered by this the moment it appears in the payload, with no test edit."""
    exercised = set()
    for d, o, r, got in _all_cells():
        blind = {k: v for k, v in got["ddl_detection"].items()
                 if k.endswith("_schemas") and v}
        exercised |= set(blind)
        if blind:
            assert got["status"] != "no_changes", (d, o, r, blind)
            assert _NEUTRAL not in panel_verdict(got), (d, o, r, blind)
            # and the operator is told WHICH schemas, by name
            for key, names in blind.items():
                for n in names:
                    assert n in got["note"], (d, o, r, key, got["note"])
    # Not vacuous: every list the payload can carry is actually driven non-empty
    # somewhere in the product.
    assert exercised == {"baseline_only_schemas", "partial_window_schemas",
                         "outside_window_schemas", "unconfirmed_schemas"}, \
        sorted(exercised)


def test_outside_window_does_not_headline_as_no_changes():
    """FINDING 3, isolated. The last round reported this cell as
    `insufficient_history`; it was `no_changes`, because whenever table_stats
    compared any pair the old `elif ddl_status == "ok" or rows_status == "ok"`
    won. ddl_detection.status was right and the note named the schema, but the
    HEADLINE an operator reads said the window was quiet."""
    got = drive(snaps=[_snap(schema="ancient", is_latest=True)],
                stats=[_stat(base=1000, cur=1000)])
    assert got["ddl_detection"]["status"] == "outside_window"
    assert got["ddl_detection"]["outside_window_schemas"] == ["ancient"]
    assert got["row_deltas"]["status"] == "ok"
    assert got["status"] == "partial", got["status"]
    jsx = panel_verdict(got)
    assert _NEUTRAL not in jsx
    assert "변경 없음이라고 볼 수 없음" in jsx
    # The note still has to name the schema: the headline says "partial", the note
    # says which schema and why.
    assert "ancient" in got["note"]
    assert "구간보다 오래되어" in got["note"]


def test_a_blind_schema_beside_a_compared_one_is_not_a_negative():
    """The relocation this round is watching for: `ddl_status == "ok"` only means
    at least ONE schema compared. With 2 schemas of which 1 is entirely outside
    the window, status ok + no rows in `changes` used to headline as no_changes,
    reporting the schema nobody could look at as unchanged."""
    got = drive(snaps=[_snap(schema="seen", stored=4),
                       _snap(schema="unseen", is_latest=True, stored=4)],
                stats=[_stat(base=1000, cur=1000)])
    assert got["ddl_detection"]["status"] == "ok"
    assert got["ddl_detection"]["schemas_compared"] == 1
    assert got["ddl_detection"]["outside_window_schemas"] == ["unseen"]
    assert got["status"] == "partial", got["status"]
    assert _NEUTRAL not in panel_verdict(got)
    assert "unseen" in got["note"]


def test_a_baseline_only_schema_beside_a_compared_one_is_named_in_the_note():
    """Same shape for baseline_only, whose note used to be keyed to
    `ddl_status == "baseline_only"` and therefore said NOTHING when another
    schema pushed ddl_status to "ok"."""
    got = drive(snaps=[_snap(schema="seen", stored=3),
                       _snap(schema="fresh_s", n=1, stored=3)],
                stats=[_stat(base=1000, cur=1000)])
    assert got["ddl_detection"]["status"] == "ok"
    assert got["ddl_detection"]["baseline_only_schemas"] == ["fresh_s"]
    assert got["status"] == "partial"
    assert "fresh_s" in got["note"], got["note"]
    assert "스냅샷 2개가" in got["note"]


def test_no_changes_stays_reachable():
    """A guard nothing can reach is the pass-3 defect. `no_changes` now demands
    ddl_complete AND rows ok, so it has to be DRIVEN, not read."""
    got = drive(snaps=[_snap()], stats=[_stat(base=1000, cur=1000)])
    assert got["status"] == "no_changes"
    assert _NEUTRAL in panel_verdict(got)


def test_the_five_statuses_read_differently_from_each_other():
    """Pairwise distinct sentences. Two states rendering the same text is the
    collapse every pass over this surface has reintroduced somewhere."""
    seen = {}
    for status in ("no_changes", "partial", "not_collected", "insufficient_history",
                   "some_status_this_panel_has_never_heard_of"):
        jsx = panel_verdict({"status": status})
        head = re.search(r'<div className="text-(?:zinc-400|amber-300)">(.*?)</div>',
                         jsx)
        assert head, (status, jsx)
        text = head.group(1).strip()
        assert text not in seen, f"{status} renders the same sentence as {seen.get(text)}"
        seen[text] = status
    assert len(seen) == 5


def test_only_no_changes_reaches_the_absence_of_change_sentence():
    for status in ("partial", "not_collected", "insufficient_history", "ok", "zzz"):
        assert _NEUTRAL not in panel_verdict({"status": status}), status
    assert _OLD_UNQUALIFIED not in _PANEL, "the unqualified empty state is back"


def test_an_unknown_status_is_unknown_and_not_a_clean_bill_of_health():
    """Deploy skew in the other direction from the anomalies panel: this is a
    static export, so an api Lambda NEWER than the bundle can send a status the
    panel has never heard of."""
    jsx = panel_verdict({"status": "some_future_status"})
    assert "알 수 없는 응답 상태" in jsx
    assert _NEUTRAL not in jsx


def test_the_two_counting_states_show_what_was_compared():
    """`no_changes` and `partial` are the states where "how much was actually
    compared" is the operator's next question, so both carry the counts line."""
    for status in ("no_changes", "partial"):
        assert "{measured}" in panel_verdict({"status": status}), status
    body = _verdict_body()
    counts = body[body.index("const measured = ("):body.index("if (d.status")]
    assert "d.ddl_detection?.schemas_compared" in counts
    assert "d.row_deltas?.tables_compared" in counts
    assert "DDL 비교" in counts and "행 수 비교" in counts


def test_the_verdict_component_is_reached_from_the_render():
    """A branch chain nobody calls is the "fix that lands in a payload nobody
    consumes" in component form."""
    site = _PANEL[_PANEL.index("shown === 0 ? ("):]
    site = site[:site.index(") : (")]
    assert "<EmptyVerdict d={data} />" in site, (
        "the empty-list path no longer renders EmptyVerdict, so none of the "
        "per-status sentences can reach the operator"
    )


def test_a_failed_fetch_is_its_own_branch_and_not_an_absence_of_change():
    """The fetch is the fourth signal and the panel owns it outright: the API is
    never called, so no status exists. It must not fall into the verdict chain."""
    branch = _PANEL[_PANEL.index("error || !data ? ("):]
    branch = branch[:branch.index("\n      ) : (")]
    assert "스키마 변경 조회 실패" in branch
    assert "변경이 없다는 뜻이 아닙니다" in branch
    assert _NEUTRAL not in branch
    assert "<EmptyVerdict" not in branch
    # The .catch must clear the data, or a stale successful payload would still
    # be rendered underneath an error.
    assert "setData(null)" in _PANEL and "setError(true)" in _PANEL


# ===========================================================================
# THE POSITIVE HALF: the branch that renders the changes THEMSELVES
# ===========================================================================
# Everything above this line is about an EMPTY list. FINDING 1 of the fifth pass:
# the empty half was modelled and the positive half was modelled by NOTHING, so
# the defect pass 1 started from (a real DROP rendered as nothing at all) was
# reintroducible with a green suite. Measured before these tests existed:
#   * delete the `{c.change_type === "dropped" && ...}` cell  -> 2331 passed
#   * replace the whole list branch with an empty <div>       -> 2331 passed
# i.e. both halves of the pass-1 defect, green.


def _emitted_change_types() -> set:
    """Every `change_type` the handler puts in `changes`."""
    start = _HANDLER_SRC.index("def _schema_changes(")
    body = _HANDLER_SRC[start:_HANDLER_SRC.index("\ndef ", start + 1)]
    found = set(re.findall(r'"change_type": "(\w+)"', body))
    assert found, "no change_type literals found in _schema_changes"
    return found


def test_every_change_type_the_server_emits_renders_something():
    """The server emits three. Each has to reach exactly one cell of ChangeRow AND
    have a TYPE_STYLES entry, or a real change reaches the operator as a row with
    no icon, no colour and no counts."""
    styles = set(re.findall(r"^    (\w+): \{ color:", _PANEL, re.M))
    for t in _emitted_change_types():
        jsx = panel_change_row({"change_type": t})
        assert jsx, t
        assert t in styles, (f"{t} has no TYPE_STYLES entry", sorted(styles))


def test_a_dropped_row_shows_the_count_it_lost():
    """The pass-1 cell, pinned at the point the operator reads it. `current_rows`
    is None for every dropped row by construction, so a cell reading `current`
    would render "행 수 미상" for every DROP that ever happens."""
    cell = panel_change_row({"change_type": "dropped"})
    assert "value={baseline}" in cell, cell
    assert "value={current}" not in cell, cell
    assert "행 손실" in cell, cell


def test_the_three_change_types_read_differently_from_each_other():
    seen = {}
    for t in ("created", "dropped", "changed"):
        cell = panel_change_row({"change_type": t})
        assert cell not in seen, f"{t} renders the same cell as {seen[cell]}"
        seen[cell] = t
    assert len(seen) == 3


def test_an_unknown_change_type_is_marked_unknown_and_not_silent():
    """Deploy skew in the positive half, the same way EmptyVerdict handles it in
    the empty half: this is a static export, so an api Lambda newer than the
    bundle can send a change_type it has never heard of (compute_diff already
    computes a `modified` list this tier does not surface yet). Before the
    fallthrough existed, such a row rendered its name and NOTHING else."""
    for t in ("modified", "renamed", "some_future_type"):
        cell = panel_change_row({"change_type": t})
        assert "{UNKNOWN_TYPE}" in cell, (t, cell)
    for t in ("created", "dropped", "changed"):
        assert "{UNKNOWN_TYPE}" not in panel_change_row({"change_type": t}), t
    # and the const it renders says so in words the operator reads
    assert 'const UNKNOWN_TYPE = "알 수 없는 변경 유형";' in _PANEL
    assert 'const KNOWN_CHANGE = ["created", "dropped", "changed"];' in _PANEL, (
        "the fallthrough is keyed to KNOWN_CHANGE, so that list has to be exactly "
        "the change types the cells above cover"
    )


def test_a_change_row_always_carries_its_identity():
    """The counts are the cells; the NAME is outside them. A row that lost its
    name would be a change nobody can act on."""
    body = _flat(_changerow_body())
    assert "{c.schema_name}" in body and "{c.table_name}" in body
    assert "{c.change_type}" in body, "the row never prints which kind of change"


def test_the_list_branch_renders_both_kinds_of_change():
    """The other half of the mutation that stayed green: the ternary's non-empty
    operand. `changes` and `rename_candidates` are two different shapes and both
    are schema changes, so both have to be mapped, and `shown` has to count both
    or a rename-only window falls into EmptyVerdict and reads as undeterminable."""
    branch = _list_branch()
    assert "changes.map" in branch, branch
    assert "<ChangeRow" in branch, branch
    assert "renames.map" in branch, branch
    flat = _flat(_PANEL)
    assert "const shown = changes.length + renames.length;" in flat
    assert "const renames = ddl?.rename_candidates ?? [];" in flat


@pytest.mark.parametrize("cell", [c for c in _MATRIX if c[2][0] == "ok"],
                         ids=[c[0] for c in _MATRIX if c[2][0] == "ok"])
def test_a_real_change_reaches_a_cell_that_says_what_happened(cell):
    """End to end for the POSITIVE half: the rows the SHIPPED derivation produced,
    each followed to the JSX ChangeRow reaches for it. This is the assertion that
    goes red when the branch rendering a dropped row is deleted."""
    _id, kwargs, _expected, _p, _f = cell
    got = drive(**kwargs)
    assert got["changes"] or got["ddl_detection"]["rename_candidates"], got
    for c in got["changes"]:
        jsx = panel_change_row(c)
        assert jsx and "알 수 없는 변경 유형" not in jsx, (c, jsx)


def test_a_dropped_row_from_the_shipped_derivation_shows_its_lost_rows():
    """The full pass-1 path in one test: a table present in the baseline snapshot
    and absent from the latest one, through compute_diff, to the cell that prints
    how many rows went with it."""
    got = drive(snaps=[_snap(before={"users": ["id"], "orders": ["id"]},
                             after={"users": ["id"]})],
                stats=[_stat(table="orders", base=9000, cur=9000)])
    row = [c for c in got["changes"] if c["table_name"] == "orders"]
    assert row and row[0]["change_type"] == "dropped", got["changes"]
    assert row[0]["baseline_rows"] == 9000, row[0]
    cell = panel_change_row(row[0])
    assert "행 손실" in cell and "value={baseline}" in cell, cell


# ===========================================================================
# The panel cannot go stale against the server
# ===========================================================================

def _emitted(var: str) -> set:
    """Every literal the handler assigns to `var` inside `_schema_changes`. A new
    status value server-side therefore fails the panel guards below rather than
    rendering as the fallthrough on a real cluster."""
    start = _HANDLER_SRC.index("def _schema_changes(")
    body = _HANDLER_SRC[start:_HANDLER_SRC.index("\ndef ", start + 1)]
    found = set(re.findall(rf'^\s+{var} = "(\w+)"', body, re.M))
    assert found, f"no `{var} = \"...\"` assignments found in _schema_changes"
    return found


def test_every_top_level_status_the_server_can_emit_has_a_branch():
    modelled = {c.split('"')[1] for c in _PREDICATES}
    emitted = _emitted("status")
    # `ok` never reaches EmptyVerdict: it means `changes` or `rename_candidates`
    # is non-empty, so the panel takes the list branch
    # (test_a_change_is_never_rendered_by_the_empty_verdict_at_all pins that).
    assert emitted - {"ok"} == modelled, (
        f"server emits {sorted(emitted - {'ok'})}, panel branches on "
        f"{sorted(modelled)}"
    )


def _chip_map(name: str) -> dict:
    start = _PANEL.index(f"const {name}: Record<")
    body = _PANEL[start:_PANEL.index("};", start)]
    out = dict(re.findall(r"^\s{2}(\w+): \{ label: \"(.*?)\", ok: (?:true|false) \},",
                          body, re.M))
    assert out, f"{name} parsed empty: the panel's shape changed"
    return out


@pytest.mark.parametrize("chip,var", [
    ("DDL_CHIP", "ddl_status"),
    ("ROWS_CHIP", "rows_status"),
    ("COLLECTION_CHIP", "collection"),
])
def test_every_source_status_the_server_can_emit_has_a_chip(chip, var):
    """The three per-source chips are what tells the operator WHICH signal was
    blind once the headline says `partial`. A server-side value with no chip
    entry falls back to the UNKNOWN chip, which is safe but says the wrong
    thing."""
    assert set(_chip_map(chip)) == _emitted(var), (
        f"{chip} keys {sorted(_chip_map(chip))} vs server {sorted(_emitted(var))}")


def test_exactly_one_chip_entry_per_source_is_marked_ok():
    """`ok: true` is what draws the chip in neutral grey instead of amber, so a
    second `ok: true` on a blind state would render blindness as normal."""
    for name in ("DDL_CHIP", "ROWS_CHIP", "COLLECTION_CHIP"):
        start = _PANEL.index(f"const {name}: Record<")
        body = _PANEL[start:_PANEL.index("};", start)]
        oks = re.findall(r"^\s{2}(\w+): \{ label: \".*?\", ok: true \},", body, re.M)
        assert oks == ["ok"] if name != "COLLECTION_CHIP" else oks == ["fresh"], (name, oks)


def test_the_four_chips_are_rendered_and_fall_back_to_the_unknown_entry():
    block = _PANEL[_PANEL.index("flex flex-wrap items-center gap-1.5"):]
    block = _flat(block[:block.index("</div>")])
    # FOUR, not three. "did the schema change" and "is the schema still there" are
    # different questions and the panel had a chip for only the first, which is why
    # `not_seen` could reach the operator as "no changes detected".
    assert block.count("<Chip") == 4, block
    assert 'OBSERVATION_CHIP[obs?.status ?? "unavailable"] ?? OBSERVATION_CHIP.unavailable' \
        in block
    # Each chip reads its own field, and a MISSING field falls back to a non-ok
    # entry rather than to the ok one.
    assert 'DDL_CHIP[ddl?.status ?? "unavailable"] ?? DDL_CHIP.unavailable' in block
    assert "ROWS_CHIP[rowStatus] ?? ROWS_CHIP.no_data" in block
    assert 'COLLECTION_CHIP[coll?.status ?? "no_data"] ?? COLLECTION_CHIP.no_data' in block
    assert 'rowStatus = data?.row_deltas?.status ?? "no_data"' in _flat(_PANEL)


def test_the_note_the_server_wrote_is_rendered():
    """Every "this is not an absence of change" sentence the handler composes
    lives in `note`. A payload field nobody renders is not a fix."""
    assert "{data.note}" in _PANEL
    got = drive(age_sec=_DEAD)
    assert got["note"], "the never-collected cell has no note to render"


# A field the panel does not read reaches the operator only if the handler folds
# it into `note`. These four reach them NOWHERE, and that is a decision, not an
# oversight: a row count, a timestamp, the freshness threshold itself and the
# cluster id the panel passed in are diagnostics, and the operator-facing meaning
# of each is already carried by a status, a chip or the note. Listing them here
# rather than asserting a promise is the mechanism: a NEW payload field that
# nobody renders fails this test, so "the fix landed in a payload nobody
# consumes" cannot happen again quietly.
_NOT_SURFACED = {
    "cluster_id",
    "snapshots_stored",
    "last_snapshot",
    "fresh_within_minutes",
    # Diagnostics for whoever fixes the collector, not for the operator: WHICH
    # catalog the history describes, HOW MANY schemas are known, and the freshness
    # bar itself. The operator-facing meaning of all three is already carried by the
    # observation chip and by the not-seen line, which names the schemas and the
    # last confirmed time.
    "read_scope",
    "schemas_known",
    "confirm_within_minutes",
}
# Not read by the panel, but folded into `note`, which is.
_VIA_NOTE = {
    "first_snapshot", "baseline_only_schemas", "partial_window_schemas",
    "outside_window_schemas", "largest_tables_only", "last_collected",
}


def test_no_payload_field_reaches_the_operator_by_accident():
    def leaves(d, pre=""):
        for k, v in d.items():
            if isinstance(v, dict):
                yield from leaves(v, f"{pre}{k}.")
            else:
                yield f"{pre}{k}", k
    got = drive(age_sec=_DEAD)
    unread = {leaf for path, leaf in leaves(got) if leaf not in _PANEL}
    assert unread == _NOT_SURFACED | _VIA_NOTE, (
        "a payload field changed its rendering status. New field the panel does "
        "not read? Either render it, fold it into `note`, or add it to "
        f"_NOT_SURFACED with the reason. Diff: {sorted(unread ^ (_NOT_SURFACED | _VIA_NOTE))}"
    )
    # The _VIA_NOTE half is a claim about the handler, so check it there.
    for name in ("first_snapshot", "baseline_only_schemas", "outside_window_schemas"):
        assert name in _HANDLER_SRC


# ===========================================================================
# THE OBSERVATION AXIS: no signal value outside the matrix
# ===========================================================================
# FINDING 2 of the sixth pass was not that a cell was wrong. It was that the
# producer's `not_seen` state was a FIFTH signal value that appeared in NO row of
# this matrix, so the matrix guard, written specifically to stop a signal value
# escaping, was structurally unable to see it. The fix is that the enumeration is
# derived from the SHARED probe rather than written beside it.

def _observation_statuses():
    """Read the enumeration off the contract module, not off a list here. api/
    cannot import mcp_servers, so the panel's copy is the one that ships with it."""
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "_panel_states_contract", _DASHBOARD_DIR / "schema_diff_util.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return set(mod.OBSERVATION_STATUSES)


def test_every_observation_status_appears_in_the_product():
    """Every value the shared probe can return is a cell of the product. A new one
    fails HERE, with no test edit, which is the guard `not_seen` walked past.

    And it is asserted against the DRIVEN payloads, not against the table: a value
    listed in _REACHABLE that the shipped code cannot actually produce would make the
    enumeration look complete while covering nothing."""
    listed = {o for obs in _REACHABLE.values() for o in obs}
    assert listed == _observation_statuses(), (
        f"observation values in the product: {sorted(listed)}, values the probe can "
        f"return: {sorted(_observation_statuses())}")
    produced = {got["observation"]["status"] for _d, _o, _r, got in _all_cells()}
    assert produced == _observation_statuses(), sorted(produced)


def test_every_observation_status_has_a_chip_and_only_fresh_is_ok():
    """The chip is what tells the operator WHICH signal was blind once the headline
    says `partial`. A server-side value with no entry falls back to the unknown chip,
    which is safe but says the wrong thing; a second `ok: true` would draw blindness
    as normal."""
    chips = _chip_map("OBSERVATION_CHIP")
    assert set(chips) == _observation_statuses(), (
        f"OBSERVATION_CHIP keys {sorted(chips)} vs probe {sorted(_observation_statuses())}")
    start = _PANEL.index("const OBSERVATION_CHIP: Record<")
    body = _PANEL[start:_PANEL.index("};", start)]
    oks = re.findall(r"^\s{2}(\w+): \{ label: \".*?\", ok: true \},", body, re.M)
    assert oks == ["fresh"], oks


def test_only_a_fully_confirmed_cluster_reaches_the_absence_of_change_sentence():
    """The property in one line, driven across the whole product: `no_changes` and
    an observation that is anything but `fresh` may never coexist."""
    for d, o, r, got in _all_cells():
        if got["observation"]["status"] != "fresh":
            assert got["status"] != "no_changes", (d, o, r, got["observation"])
            assert _NEUTRAL not in panel_verdict(got), (d, o, r)


def test_a_schema_nobody_can_see_is_named_with_the_time_it_was_last_confirmed():
    """THE ACCEPTED COST, at the sentence. A genuine DROP SCHEMA is not reported as
    a drop anywhere; it has to read as "last confirmed at T, not seen since" and it
    has to reach the operator, which is what the fifth pass left out of this panel
    entirely."""
    got = drive(snaps=[_snap(schema="live_s", stored=4)],
                obs=[_obs_row("live_s"), _obs_row("gone_s", confirmed=False)],
                stats=[_stat(base=1000, cur=1000)])
    assert got["status"] == "partial"
    assert got["ddl_detection"]["unconfirmed_schemas"] == ["gone_s"]
    assert got["observation"]["status"] == "not_seen"
    assert got["observation"]["last_confirmed"] == _CONFIRMED_AT
    # the note names it, and never as a drop
    assert "gone_s" in got["note"]
    assert "확인되지 않았습니다" in got["note"]
    assert "삭제로 단정하지 않고" in got["note"]
    for drop_word in ("삭제됨", "dropped"):
        assert drop_word not in got["note"], drop_word
    # and the panel renders that, in every branch of the verdict chain.
    # The BINDING is asserted as well as the interpolation: checking only
    # `{notSeen}` was MUTATION-CHECKED and survived `const notSeen = null;`, which
    # deletes the whole operator-facing half while leaving every interpolation in
    # place (measured: 661 passed).
    assert "const notSeen = <NotSeen d={d} />;" in _verdict_body(), (
        "the not-seen line is no longer bound to the NotSeen component, so every "
        "`{notSeen}` below it renders nothing"
    )
    assert "{notSeen}" in panel_verdict(got)
    for status in ("no_changes", "partial", "not_collected", "insufficient_history",
                   "some_future_status"):
        assert "{notSeen}" in panel_verdict({"status": status}), status
    # the component itself prints the names and the timestamp
    body = _PANEL[_PANEL.index("function NotSeen("):_PANEL.index("// The verdict an EMPTY")]
    assert "unconfirmed_schemas" in body
    assert "last_confirmed" in body
    assert "확인되지 않았습니다" in body
    assert "if (names.length === 0) return null;" in body, (
        "a fully confirmed cluster must gain no chrome at all")


def test_the_not_seen_line_also_reaches_a_window_that_has_real_changes():
    """EmptyVerdict is not reached when there are rows, so a window with one real
    change plus an unconfirmed schema would hide the unknown behind the change."""
    got = drive(snaps=[_snap(schema="live_s", stored=4,
                             before={"users": ["id"]},
                             after={"users": ["id"], "made": ["a", "b"]})],
                obs=[_obs_row("live_s"), _obs_row("gone_s", confirmed=False)],
                stats=[_stat(base=1000, cur=1000)])
    assert got["status"] == "ok"
    assert got["changes"], got
    assert got["ddl_detection"]["unconfirmed_schemas"] == ["gone_s"]
    branch = _list_branch()
    tail = _flat(_PANEL[_PANEL.index("{shown === 0 ? ("):])
    assert "<NotSeen d={data} />" in tail, (
        "the list branch renders no not-seen line, so a window containing one real "
        "change hides a schema nobody can see any more"
    )
    assert "changes.map" in branch


def test_the_panel_never_calls_a_vanished_schema_dropped():
    """The cost is accepted AND kept visible: `dropped` is a change_type the panel
    draws in red, and a schema nobody can confirm must never reach it. Driven: the
    unconfirmed schema contributes no row of any kind."""
    got = drive(snaps=[_snap(schema="live_s", stored=4)],
                obs=[_obs_row("live_s"), _obs_row("gone_s", confirmed=False)],
                stats=[_stat(base=1000, cur=1000)])
    assert [c for c in got["changes"] if c["schema_name"] == "gone_s"] == []
    assert got["ddl_detection"]["rename_candidates"] == []


def test_a_refused_dialect_never_draws_a_created_or_dropped_chip():
    """FINDING 4 at the surface that DRAWS the claim. A `created` / `dropped` row is
    what a DBA acts on, and on a privilege-filtered catalog it could be a permission
    change, so the panel must contribute no row at all and say why.

    MEASURED on a real mysqld 9.3.0 with the read the collector used to run, as the
    collecting identity: `REVOKE SELECT ON appdb.*` removed `orders` from the read,
    `GRANT SELECT (id)` removed the COLUMN `email`, revoking the database removed the
    schema entirely, and read_scope stayed `collector@localhost` through all of it.
    """
    got = drive(snaps=[], engine="aurora-mysql", stats=[_stat(base=1000, cur=1000)])
    assert got["ddl_detection"]["status"] == "not_supported", got["ddl_detection"]
    assert got["observation"]["status"] == "unsupported_engine"
    assert got["changes"] == [] and got["ddl_detection"]["rename_candidates"] == []
    assert got["status"] == "partial"
    # The refusal is stated, and the young-cluster promise is NOT. The sentence names
    # the POSITIVE rule rather than MySQL's mechanism: the same gate refuses four
    # other families whose reason is not MySQL's (eighth pass, FINDING 4).
    assert "PostgreSQL" in got["note"] and "pg_namespace" in got["note"], got["note"]
    assert "다음 ETL 주기에 최초 baseline" not in got["note"]
    # A DocumentDB cluster is refused by the same gate and must not be handed MySQL's
    # reason for its own cluster.
    docdb = drive(snaps=[], engine="docdb", stats=[_stat(base=1000, cur=1000)])
    assert docdb["ddl_detection"]["status"] == "not_supported", docdb["ddl_detection"]
    for one_familys_reason in ("MySQL", "REVOKE", "information_schema"):
        assert one_familys_reason not in docdb["note"], one_familys_reason
    assert _NEUTRAL not in panel_verdict(got)
    # Even with stored rows from before the refusal, no pair is selected: the mock
    # returns snapshot rows and the payload still carries no change.
    with_legacy = drive(snaps=[_snap(before={"users": ["id"], "gone": ["id"]},
                                     after={"users": ["id"]})],
                        engine="mysql", stats=[_stat(base=1000, cur=1000)])
    assert with_legacy["changes"] == [], with_legacy["changes"]
    assert with_legacy["ddl_detection"]["status"] == "not_supported"


def test_the_refusal_and_the_unknown_engine_read_differently():
    """FAIL-CLOSED without over-claiming: a cluster whose cluster_meta row has not
    landed yet is `unavailable` (we could not decide), not "this engine is not
    supported", and the two sentences name different things to check."""
    refused = drive(snaps=[], engine="aurora-mysql",
                    stats=[_stat(base=1000, cur=1000)])
    unknown = drive(snaps=[_snap()], engine=None, stats=[_stat(base=1000, cur=1000)])
    assert refused["ddl_detection"]["status"] == "not_supported"
    assert unknown["ddl_detection"]["status"] == "unavailable"
    assert "cluster_meta" in unknown["note"], unknown["note"]
    assert "REVOKE" not in unknown["note"]
    assert refused["note"] != unknown["note"]
