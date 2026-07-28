"""`GET /api/dashboard/{id}/anomalies` must say WHY the list is empty.

An empty `anomalies` list has FOUR meanings and only one of them is good news:

  * baselines exist, nothing crossed the threshold  -> genuinely quiet
  * no baseline matched, but samples ARE arriving   -> nothing scored, WAIT
  * no recent cluster-level samples at all          -> nothing scored, go FIX
                                                       collection
  * the lookup failed                               -> unknown

Before this, `_anomalies` returned only {cluster_id, hours, threshold,
anomalies}, so the panel rendered "최근 4시간 동안 이상 징후 없음" for all four.
The reviewer reproduced the bad state on real PostgreSQL: 0 `metric_baselines`
rows, 0 metrics scored, and a DocumentDB operator told there were no anomalies.

`baseline_mode` + `total_checked` close it, derived exactly the way
detect_anomalies_impl derives them, off the SAME (verbatim-copied) SQL. Two
follow-up defects, both confirmed in the shipped code, are pinned here too:

  * `none` collapsed "wait for history" with "your collector is dead", because
    the scoring query is DRIVEN from its `recent` CTE. The 4th state
    (`no_samples`) comes from an existence probe that runs ONLY when scoring
    returned nothing.
  * `total_checked` / `has_seasonal` were derived AFTER `LIMIT 50`, so above 50
    scored metrics the count silently capped and a seasonal baseline outside the
    top 50 by |z| reported mode `flat`. The scoring SQL is now unlimited and the
    cap applies to the DISPLAYED list only.
"""

import importlib.util
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_DASHBOARD_DIR = _ROOT / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_spec = importlib.util.spec_from_file_location(
    "dashboard_handler_anomalies", _DASHBOARD_DIR / "handler.py"
)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)

sys.path.insert(0, str(_ROOT / "mcp-servers"))
from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl  # noqa: E402

# One recent cluster-level sample, as the existence probe (`SELECT 1 ... LIMIT
# 1`) sees it. Truthy is all the reader looks at.
_A_SAMPLE = [{"?column?": 1}]


def _apply_limit(sql, rows):
    """Truncate the way the SQL's own LIMIT clause would.

    Modelled, not ignored: a fake that replays its canned rows whatever the SQL
    says cannot see a `LIMIT 50` come back into the scoring query, which is
    exactly the defect the >50 fixture below exists to catch. Same idea as
    `_apply_dim_filter` in test_metric_filters.py.
    """
    m = re.search(r"\bLIMIT\s+(\d+)", sql, re.I)
    return rows[: int(m.group(1))] if m else rows


def _query(scored, recent=()):
    """Records the SQL it was handed and replays canned rows.

    `scored` answers the scoring query (the one that reads metric_baselines),
    `recent` answers the recent-samples existence probe. Default: no recent
    samples, so a test that expects `none` has to say so.
    """
    calls = []

    def query(sql, params=None):
        calls.append((sql, params or {}))
        rows = scored if "metric_baselines" in sql else list(recent)
        return _apply_limit(sql, rows)

    query.calls = calls
    return query


class _Cache:
    """CacheClient stand-in with the same routing + LIMIT model."""

    class _Result:
        def __init__(self, rows):
            self.rows = rows

    def __init__(self, scored, recent=()):
        self._scored = scored
        self._recent = recent
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))
        rows = self._scored if "metric_baselines" in sql else list(self._recent)
        return self._Result(_apply_limit(sql, rows))


def _row(metric, z, mode, **kw):
    # Data API hands numerics back as strings (PG numeric -> stringValue), which
    # is exactly why the threshold comparison has to coerce.
    return {
        "metric_type": metric,
        "recent_max": "100",
        "recent_avg": "80",
        "baseline_mean": "10",
        "baseline_stddev": "2",
        "z_score": str(z),
        "mode": mode,
        "sample_count": kw.get("sample_count"),
    }


def _wide_fixture():
    """The exact shape of defect 2: MORE than 50 scored metrics, ordered |z|
    descending as the SQL orders them, with the ONLY seasonal baseline outside
    the top 50.

    56 is deliberately BEYOND what today's collectors produce, not a state a
    cluster is in now: counted off the shipped collector tables the deepest family
    reaches about 30 cluster-level metric_types (Aurora PG), and pg_stat_database
    + bgwriter contribute 4 of those, not a dozen. This fixture is the future the
    removed LIMIT protects against, which is why the cap has to be a display cap.

    Derived-after-LIMIT reports total_checked 50 and mode 'flat'. The truth is
    56 and 'seasonal'.
    """
    rows = [_row(f"m{i}", 100 - i, "flat") for i in range(55)]
    rows.append(_row("pg_stat_database_blks_hit", 0.2, "seasonal", sample_count=140))
    return rows


# ---------------------------------------------------------------------------
# Verbatim-copy contract
# ---------------------------------------------------------------------------


def _grab_sql(path, name):
    m = re.search(rf'{name} = f?"""(.*?)"""', path.read_text(encoding="utf-8"), re.S)
    assert m, f"{name} not found in {path}"
    return m.group(1)


_MCP_TOOL = _ROOT / "mcp-servers/mcp_servers/performance/tools/detect_anomalies.py"


def test_the_scoring_sql_is_byte_identical_to_the_mcp_tools():
    """api/ cannot import mcp_servers, so the SQL is duplicated. If the two
    drift, the dashboard and the chat agent disagree about whether a metric is
    anomalous AND about whether the cluster has a trained baseline at all."""
    for name in ("_ANOMALY_SQL", "_RECENT_SAMPLES_SQL"):
        assert _grab_sql(_DASHBOARD_DIR / "handler.py", name) == _grab_sql(_MCP_TOOL, name), name


def test_both_surfaces_execute_the_same_sql_text():
    """Stronger than source identity: the probe interpolates CLUSTER_LEVEL_ONLY
    from each package's own copy of metric_filters, so compare what actually
    reaches the database."""
    q = _query([])
    handler._anomalies(q, "c1", 4, 2.5)
    cache = _Cache([])
    detect_anomalies_impl(cache, "c1", 4, 2.5)
    assert [s for s, _ in q.calls] == [s for s, _ in cache.calls]
    assert len(q.calls) == 2, "scoring query + existence probe"


def test_scoring_sql_does_not_filter_by_threshold_or_limit_and_keeps_both_dim_filters():
    """The threshold filter must NOT be in SQL: rows below it are the evidence
    that scoring happened at all. Neither may a LIMIT: total_checked and the
    seasonal/flat decision are derived from every row it returns. And both
    metric_snapshots reads stay cluster-level only (strict dimension filter)."""
    q = _query([])
    handler._anomalies(q, "c1", 4, 2.5)
    sql, params = q.calls[0]
    assert ":threshold" not in sql
    assert not re.search(r"\bLIMIT\b", sql, re.I), (
        "a LIMIT on the scoring query caps total_checked and can hide the only "
        "seasonal baseline outside the top-N by |z|"
    )
    assert sql.count("dimensions IS NULL OR dimensions::text = '{}'") == 2
    assert params == {"cluster_id": "c1", "hours": "4"}


def test_the_probe_reads_the_same_window_as_the_scoring_query():
    """A probe over a different window could contradict the scoring query it is
    disambiguating."""
    q = _query([])
    handler._anomalies(q, "c1", 6, 2.5)
    probe_sql, probe_params = q.calls[1]
    assert "(:hours || ' hours')::interval" in probe_sql
    assert probe_params == {"cluster_id": "c1", "hours": "6"}
    assert probe_params == q.calls[0][1]


# ---------------------------------------------------------------------------
# baseline_mode: the four states
# ---------------------------------------------------------------------------


def test_zero_recent_samples_reports_no_samples_not_a_cold_start():
    """DEFECT 1. Nothing scored AND nothing collected: the ETL is broken or the
    cluster was just registered. Reporting `none` here tells the operator to
    wait about two weeks for a baseline while the collector is dead."""
    out = handler._anomalies(_query([], recent=[]), "docdb-1", 4, 2.5)
    assert out["baseline_mode"] == "no_samples"
    assert out["total_checked"] == 0
    assert out["anomalies"] == []


def test_recent_samples_but_no_baseline_still_reports_none():
    """The reviewer's original reproduced state: samples arriving, 0
    metric_baselines rows and no 7-day flat baseline either, so scoring had
    nothing to compare against. Waiting for history IS the right advice here."""
    out = handler._anomalies(_query([], recent=_A_SAMPLE), "docdb-1", 4, 2.5)
    assert out["baseline_mode"] == "none"
    assert out["total_checked"] == 0


def test_the_probe_runs_only_when_nothing_was_scored():
    """It exists to disambiguate an EMPTY result; on the normal path it must not
    cost a round trip."""
    q = _query([_row("cpu", 0.4, "seasonal")], recent=_A_SAMPLE)
    handler._anomalies(q, "c1", 4, 2.5)
    assert len(q.calls) == 1
    assert "metric_baselines" in q.calls[0][0]


def test_quiet_cluster_with_a_trained_baseline_is_distinguishable_from_mode_none():
    """THE regression this exists for: every scored metric is below threshold, so
    `anomalies` is empty, but a seasonal baseline demonstrably exists. Deriving
    baseline_mode from the FILTERED list would report 'none' here and the panel
    would tell the operator no baseline is trained yet."""
    rows = [_row("cpu", 0.4, "seasonal"), _row("aas", 1.1, "seasonal")]
    out = handler._anomalies(_query(rows), "c1", 4, 2.5)
    assert out["anomalies"] == []
    assert out["baseline_mode"] == "seasonal"
    assert out["total_checked"] == 2


def test_only_flat_rows_report_mode_flat():
    out = handler._anomalies(_query([_row("cpu", 0.2, "flat")]), "c1", 4, 2.5)
    assert out["baseline_mode"] == "flat"


def test_any_seasonal_row_wins_over_flat_rows():
    rows = [_row("cpu", 3.0, "flat"), _row("aas", 0.1, "seasonal")]
    assert handler._anomalies(_query(rows), "c1", 4, 2.5)["baseline_mode"] == "seasonal"


# ---------------------------------------------------------------------------
# Threshold filtering + display cap (defect 2)
# ---------------------------------------------------------------------------


def test_threshold_filters_on_absolute_z_and_counts_everything_checked():
    rows = [
        _row("cpu", 4.0, "seasonal"),
        _row("aas", -3.1, "seasonal"),   # negative anomaly still an anomaly
        _row("conn", 2.5, "seasonal"),   # exactly at threshold: included
        _row("iops", 1.0, "seasonal"),   # below: counted, not reported
    ]
    out = handler._anomalies(_query(rows), "c1", 4, 2.5)
    assert [a["metric_type"] for a in out["anomalies"]] == ["cpu", "aas", "conn"]
    assert out["total_checked"] == 4


def test_unparseable_z_score_is_not_reported_as_an_anomaly():
    out = handler._anomalies(_query([_row("cpu", "NaN-ish", "seasonal")]), "c1", 4, 2.5)
    assert out["anomalies"] == []
    assert out["baseline_mode"] == "seasonal"  # it WAS scored, just unreadable


def test_more_than_fifty_scored_metrics_count_and_classify_off_the_full_set():
    """DEFECT 2. The seasonal baseline sits at position 56 by |z|, so anything
    derived after a 50-row cut calls this cluster 'flat' and claims 50 metrics
    were checked."""
    out = handler._anomalies(_query(_wide_fixture()), "c1", 4, 2.5)
    assert out["total_checked"] == 56
    assert out["baseline_mode"] == "seasonal"


def test_the_reported_list_stays_capped_and_is_the_strongest_anomalies():
    """The cap applies to what is DISPLAYED, and the SQL's own ORDER BY ABS(z)
    DESC is what makes those the 50 STRONGEST: the rows at or above threshold are
    a prefix of a sorted list, so the cap can only drop the weakest.

    The cap's position relative to the threshold filter is NOT what buys that.
    On sorted input the two orders are provably identical, and swapping them is
    an equivalent mutation (checked: 0 failures). So this test pins the ordering
    and the size, not the placement."""
    out = handler._anomalies(_query(_wide_fixture()), "c1", 4, 2.5)
    zs = [abs(float(a["z_score"])) for a in out["anomalies"]]
    assert len(zs) == 50
    assert zs == sorted(zs, reverse=True)
    assert zs[0] == 100.0
    assert zs[-1] == 51.0        # rows 51..55 are dropped by the cap, not the top ones
    assert all(z >= 2.5 for z in zs)


def test_the_cap_never_hides_a_below_threshold_row_that_was_still_counted():
    """A cluster with 60 scored metrics of which only 3 are anomalous reports 3,
    not 50: the cap is a ceiling, not a page size."""
    rows = [_row(f"m{i}", 0.1, "flat") for i in range(60)]
    rows[0:0] = [_row("a", 9.0, "flat"), _row("b", 8.0, "flat"), _row("c", 7.0, "flat")]
    out = handler._anomalies(_query(rows), "c1", 4, 2.5)
    assert [a["metric_type"] for a in out["anomalies"]] == ["a", "b", "c"]
    assert out["total_checked"] == 63


# ---------------------------------------------------------------------------
# Both surfaces agree
# ---------------------------------------------------------------------------


def test_both_surfaces_derive_identical_signals_for_every_case():
    """Same rows through both readers must yield the same derived fields, not
    just the same SQL text. This is the contract the verbatim copy exists to
    serve: the dashboard and the chat agent must not disagree about whether the
    cluster is judgeable at all."""
    cases = [
        ([], []),                                    # nothing scored, nothing collected
        ([], _A_SAMPLE),                             # nothing scored, samples arriving
        ([_row("cpu", 0.4, "seasonal")], _A_SAMPLE),
        ([_row("cpu", 0.4, "flat")], []),
        ([_row("cpu", 9.0, "flat"), _row("aas", 0.1, "seasonal")], []),
        (_wide_fixture(), []),                       # > 50 scored, seasonal outside the cut
    ]
    for scored, recent in cases:
        label = f"{len(scored)} scored, recent={bool(recent)}"
        mine = handler._anomalies(_query(scored, recent), "c1", 4, 2.5)
        theirs = detect_anomalies_impl(_Cache(scored, recent), "c1", 4, 2.5)
        assert mine["baseline_mode"] == theirs["baseline_mode"], label
        assert mine["total_checked"] == theirs["total_checked"], label
        assert [a["metric_type"] for a in mine["anomalies"]] == [
            a["metric_type"] for a in theirs["anomalies"]
        ], label
