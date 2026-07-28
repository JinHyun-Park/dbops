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

  PANEL half   the branch chain of `EmptyVerdict` in
               frontend/src/components/dashboard/schema-changes-panel.tsx is
               PARSED and MODELLED, so the assertion is about the JSX a status
               REACHES. The previous round guarded this with `assert field in
               src`, which cannot tell a rendered branch from a type-map key or
               a comment, and it also claimed a payload/panel contract test that
               did not exist. Same idiom as
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


def _stat(table="t", base=1000, cur=1000, *, in_window=True, schema="app"):
    """One row of what _TABLE_STATS_WINDOW_SQL returns."""
    return {
        "schema_name": schema, "table_name": table,
        "baseline_rows": base, "current_rows": cur,
        "baseline_time": _T0, "current_time": _T1,
        "current_in_window": in_window,
    }


def drive(*, snaps=(), stats=(), age_sec=60, snaps_fail=False, days=7):
    """Run the SHIPPED `_schema_changes` over these rows.

    The fake dispatches on which of the three statements is being run and returns
    ROWS. It computes no status: `collection`, `ddl_detection.status`,
    `row_deltas.status` and the top-level `status` are all derived by the handler
    under test. `age_sec=None` means table_stats has no row for this cluster."""
    def query(sql, params=None):
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
    return (p["status"], p["ddl_detection"]["status"], p["row_deltas"]["status"],
            p["collection"]["status"])


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
     ("not_collected", "not_collected", "no_data", "no_data"),
     "수집 이력이 없어", [_NEUTRAL]),

    # --- the cache DB has no schema_v26 (the read raises) -----------------
    ("no_schema_v26_but_rows_compare",
     dict(snaps_fail=True, stats=[_stat(base=1000, cur=1000)]),
     ("partial", "unavailable", "ok", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("no_schema_v26_and_no_rows_either",
     dict(snaps_fail=True, stats=[_stat(base=None, cur=5)]),
     ("insufficient_history", "unavailable", "insufficient_history", "fresh"),
     "비교 가능한 이력이 부족해", [_NEUTRAL]),

    # --- snapshots entirely outside the window (FINDING 3's cell) --------
    ("outside_window_but_rows_compare",
     dict(snaps=[_snap(is_latest=True)], stats=[_stat(base=1000, cur=1000)]),
     ("partial", "outside_window", "ok", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("outside_window_and_rows_too_old",
     dict(snaps=[_snap(is_latest=True)], stats=[_stat(in_window=False)],
          age_sec=40 * 24 * 3600),
     ("insufficient_history", "outside_window", "insufficient_history", "stale"),
     "비교 가능한 이력이 부족해", [_NEUTRAL]),

    # --- a blind schema BESIDE a compared one ----------------------------
    ("one_schema_compared_one_outside_window",
     dict(snaps=[_snap(schema="ok_s", after={"users": ["id"]}, stored=4),
                 _snap(schema="blind_s", is_latest=True, stored=4)],
          stats=[_stat(base=1000, cur=1000)]),
     ("partial", "ok", "ok", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("one_schema_compared_one_baseline_only",
     dict(snaps=[_snap(schema="ok_s", stored=3),
                 _snap(schema="new_s", n=1, stored=3)],
          stats=[_stat(base=1000, cur=1000)]),
     ("partial", "ok", "ok", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),

    # --- one source silent -----------------------------------------------
    ("ddl_never_collected_rows_ok",
     dict(stats=[_stat(base=1000, cur=1000)]),
     ("partial", "not_collected", "ok", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("ddl_baseline_only_rows_ok",
     dict(snaps=[_snap(n=1, stored=1)], stats=[_stat(base=1000, cur=1000)]),
     ("partial", "baseline_only", "ok", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),
    ("ddl_ok_rows_have_one_endpoint",
     dict(snaps=[_snap()], stats=[_stat(base=None, cur=7)]),
     ("partial", "ok", "insufficient_history", "fresh"),
     "일부 신호만 판정됨", [_NEUTRAL]),

    # --- the only cell that may read as an absence of change -------------
    ("fresh_and_both_sources_compared",
     dict(snaps=[_snap()], stats=[_stat(base=1000, cur=1000)]),
     ("no_changes", "ok", "ok", "fresh"),
     _NEUTRAL, ["일부 신호만 판정됨", "판정할 수 없음"]),
    ("stale_but_still_inside_the_window",
     dict(snaps=[_snap()], stats=[_stat(base=1000, cur=1000)], age_sec=_STALE_SEC),
     ("no_changes", "ok", "ok", "stale"),
     _NEUTRAL, ["일부 신호만 판정됨"]),

    # --- real changes ----------------------------------------------------
    ("stale_with_a_real_ddl_change",
     dict(snaps=[_snap(before={"users": ["id"]},
                       after={"users": ["id"], "created_tbl": ["id"]})],
          stats=[_stat(base=1000, cur=1000)], age_sec=_STALE_SEC),
     ("ok", "ok", "ok", "stale"), None, None),
    ("fresh_with_a_real_ddl_change",
     dict(snaps=[_snap(before={"users": ["id"], "gone_tbl": ["id"]},
                       after={"users": ["id"]})],
          stats=[_stat(base=1000, cur=1000)]),
     ("ok", "ok", "ok", "fresh"), None, None),
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


def test_the_three_chips_are_rendered_and_fall_back_to_the_unknown_entry():
    block = _PANEL[_PANEL.index("flex flex-wrap items-center gap-1.5"):]
    block = _flat(block[:block.index("</div>")])
    assert block.count("<Chip") == 3, block
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
