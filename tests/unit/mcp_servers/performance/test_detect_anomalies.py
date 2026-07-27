"""detect_anomalies: the agent-facing twin of the dashboard's anomaly reader.

`baseline_mode` has four states, not three. `none` (samples arriving, no
baseline matched -> wait for history) and `no_samples` (no recent cluster-level
samples at all -> go check collection) call for opposite operator actions, and
the agent must not tell a DBA to wait two weeks while the collector is dead.

`total_checked` and the seasonal/flat classification come from the FULL scored
set: the scoring SQL carries no LIMIT and the 50-row cap applies only to the
`anomalies` list handed back.
"""

import re
from unittest.mock import MagicMock

from mcp_servers.performance.tools.detect_anomalies import detect_anomalies_impl
from mcp_servers.shared.models import QueryResult

_COLS = ["metric_type", "recent_max", "recent_avg", "baseline_mean",
         "baseline_stddev", "z_score", "mode", "sample_count"]

# One recent cluster-level sample, as the `SELECT 1 ... LIMIT 1` probe sees it.
_A_SAMPLE = [{"?column?": 1}]


def _rows(rows):
    return QueryResult(columns=_COLS, rows=rows, row_count=len(rows))


def _row(metric, z, mode, sample_count=None):
    return {"metric_type": metric, "recent_max": 100.0, "recent_avg": 80.0,
            "baseline_mean": 10.0, "baseline_stddev": 2.0, "z_score": z,
            "mode": mode, "sample_count": sample_count}


class _Cache:
    """Routes the scoring query and the existence probe to separate canned rows,
    and honours whatever LIMIT the SQL actually carries.

    The LIMIT is MODELLED on purpose: a fake that replays its rows whatever the
    SQL says could not see a `LIMIT 50` come back into the scoring query, which
    is the defect the >50 fixture below exists to catch.
    """

    def __init__(self, scored, recent=()):
        self._scored = scored
        self._recent = recent
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))
        rows = self._scored if "metric_baselines" in sql else list(self._recent)
        m = re.search(r"\bLIMIT\s+(\d+)", sql, re.I)
        if m:
            rows = rows[: int(m.group(1))]
        return _rows(rows)


def _wide_fixture():
    """> 50 scored metrics, |z| descending as the SQL orders them, with the ONLY
    seasonal baseline outside the top 50."""
    rows = [_row(f"m{i}", 100 - i, "flat") for i in range(55)]
    rows.append(_row("pg_stat_database_blks_hit", 0.2, "seasonal", 140))
    return rows


def test_detect_anomalies_filters_by_threshold():
    result = detect_anomalies_impl(
        _Cache([_row("aas", 5.5, "seasonal", 120), _row("cpu", 1.0, "seasonal", 120)]),
        cluster_id="prod-pg-1", hours=4, threshold=2.0,
    )
    assert result["cluster_id"] == "prod-pg-1"
    assert len(result["anomalies"]) == 1  # only aas (z=5.5) clears z>=2
    assert result["anomalies"][0]["metric_type"] == "aas"
    assert result["total_checked"] == 2


def test_detect_anomalies_uses_seasonal_baseline_query():
    """The query must read metric_baselines (seasonal) not just a flat stddev."""
    mock_cache = MagicMock()
    mock_cache.execute.return_value = _rows([])
    detect_anomalies_impl(mock_cache, cluster_id="prod-pg-1")
    sql = mock_cache.execute.call_args_list[0].args[0]
    assert "metric_baselines" in sql
    assert "hour_of_week" in sql
    assert "iqr" in sql.lower()


def test_the_scoring_query_carries_no_limit():
    """total_checked and the seasonal/flat decision are derived from every row
    it returns, so a LIMIT caps the count and can hide the only seasonal
    baseline outside the top-N by |z|."""
    cache = _Cache([])
    detect_anomalies_impl(cache, cluster_id="prod-pg-1")
    assert not re.search(r"\bLIMIT\b", cache.calls[0][0], re.I)
    assert "ORDER BY ABS(z_score) DESC" in cache.calls[0][0]


def test_detect_anomalies_reports_baseline_mode():
    result = detect_anomalies_impl(
        _Cache([_row("aas", 5.5, "seasonal", 120)]), cluster_id="prod-pg-1", threshold=2.0
    )
    assert result["baseline_mode"] == "seasonal"


def test_detect_anomalies_flat_fallback_mode():
    result = detect_anomalies_impl(
        _Cache([_row("connections", 5.0, "flat")]), cluster_id="prod-pg-1", threshold=2.0
    )
    assert result["baseline_mode"] == "flat"
    assert len(result["anomalies"]) == 1


def test_no_recent_samples_reports_no_samples_not_a_cold_start():
    """Nothing scored AND nothing collected. `none` here would tell the DBA to
    wait about two weeks for a baseline while the collector is dead."""
    result = detect_anomalies_impl(_Cache([], recent=[]), cluster_id="prod-pg-1")
    assert result["baseline_mode"] == "no_samples"
    assert result["anomalies"] == []
    assert result["total_checked"] == 0


def test_recent_samples_with_no_baseline_reports_none():
    """Samples are arriving, no baseline of either kind matched. Waiting for
    history IS the right advice here."""
    result = detect_anomalies_impl(_Cache([], recent=_A_SAMPLE), cluster_id="prod-pg-1")
    assert result["baseline_mode"] == "none"


def test_the_probe_carries_the_strict_cluster_level_filter_and_the_same_window():
    cache = _Cache([], recent=[])
    detect_anomalies_impl(cache, cluster_id="prod-pg-1", hours=6)
    probe_sql, probe_params = cache.calls[1]
    assert "dimensions IS NULL OR dimensions::text = '{}'" in probe_sql
    assert "(:hours || ' hours')::interval" in probe_sql
    assert probe_params == {"cluster_id": "prod-pg-1", "hours": 6}
    assert probe_params == cache.calls[0][1]


def test_the_probe_runs_only_when_nothing_was_scored():
    cache = _Cache([_row("cpu", 0.4, "seasonal")], recent=_A_SAMPLE)
    detect_anomalies_impl(cache, cluster_id="prod-pg-1")
    assert len(cache.calls) == 1


def test_more_than_fifty_scored_metrics_count_and_classify_off_the_full_set():
    """The one seasonal baseline sits at position 56 by |z|: anything derived
    after a 50-row cut calls this cluster 'flat' and claims 50 were checked."""
    result = detect_anomalies_impl(_Cache(_wide_fixture()), cluster_id="prod-pg-1", threshold=2.5)
    assert result["total_checked"] == 56
    assert result["baseline_mode"] == "seasonal"


def test_the_reported_list_stays_capped_and_holds_the_strongest_anomalies():
    result = detect_anomalies_impl(_Cache(_wide_fixture()), cluster_id="prod-pg-1", threshold=2.5)
    zs = [abs(a["z_score"]) for a in result["anomalies"]]
    assert len(zs) == 50
    assert zs == sorted(zs, reverse=True)
    assert (zs[0], zs[-1]) == (100, 51)
    assert all(z >= 2.5 for z in zs)
