"""Storage right-sizing, which applies to the rds_instance family and nothing else.

The scaffold this replaced said to "recommend shrinking via Modify-DBInstance".
That is not a thing: RDS allocated storage can only ever be INCREASED. The remedy
is a migration to a smaller instance, and advice a DBA cannot execute is worse than
no advice, so the most important assertion here is about what the recommendation
does NOT claim.

Fixture grounding, read live from the dev cache 2026-07-29:
  dbops-demo-mysql      allocated 20 GB, free_storage_bytes steady ~18.17 GB (1.8 used)
  dbops-demo-mssql      allocated 20 GB, free ~19.65 GB (0.35 used)
Both sit at RDS's 20 GB floor for gp2/gp3, so the CORRECT behaviour on this
deployment is SILENCE. A ratio check alone would have produced two findings nobody
can act on, which is the fastest way to teach a DBA to ignore the Cost tab.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_C = _ROOT / "data-pipeline/etl_collector/collectors/cost_check.py"
_spec = importlib.util.spec_from_file_location("cost_check_storage", _C)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

_SILENT_ENGINES = ["aurora-mysql", "aurora-postgresql", "docdb", "dynamodb",
                   "redis", "valkey", "memcached"]


def _meta(engine, allocated, storage_type="gp3", autoscale=None):
    rd = {"allocated_storage_gb": allocated, "storage_type": storage_type}
    if autoscale:
        rd["max_allocated_storage_gb"] = autoscale
    # resource_details arrives from the RDS Data API as a JSON STRING, which is why
    # the collector parses it rather than treating it as a dict.
    return {"engine": engine, "resource_details": json.dumps(rd)}


def _run(monkeypatch, engine, allocated, free_gb, samples=10000, **kw):
    """(finding count, captured finding fields or None)."""
    captured = {}

    def fake_emit(rds, arn, sec, db, cluster_id, check_type, severity, subject,
                  value_str, threshold_str, recommendation, details):
        captured.update(check_type=check_type, severity=severity, subject=subject,
                        value_str=value_str, recommendation=recommendation,
                        details=details)

    monkeypatch.setattr(cc, "_emit_finding", fake_emit)
    monkeypatch.setattr(cc, "_execute",
                        lambda *a, **k: [{"max_free": free_gb * (1024 ** 3),
                                          "samples": samples}])
    n = cc._check_storage_rightsize(
        None, None, None, None, "c1", _meta(engine, allocated, **kw))
    return n, (captured or None)


@pytest.mark.parametrize("engine,allocated,free_gb", [
    ("mysql", 20, 18.17),
    ("sqlserver-ex", 20, 19.65),
])
def test_an_instance_at_the_rds_floor_is_silent(monkeypatch, engine, allocated, free_gb):
    """The live fixture values. 20 GB is RDS's minimum for gp2/gp3, so there is
    nowhere smaller to migrate to."""
    n, f = _run(monkeypatch, engine, allocated, free_gb)
    assert n == 0, f


@pytest.mark.parametrize("engine", _SILENT_ENGINES)
def test_engines_without_provisioned_storage_are_silent(monkeypatch, engine):
    """Aurora and DocumentDB auto-scale and bill per GB USED, so there is no
    allocation to shrink. DynamoDB has no storage sizing at all: its analogue is
    provisioned RCU/WCU, which is a different check and a different check_type.
    ElastiCache has none either."""
    n, f = _run(monkeypatch, engine, 500, 470.0)
    assert n == 0, (engine, f)


def test_a_genuinely_over_allocated_instance_is_reported(monkeypatch):
    n, f = _run(monkeypatch, "mysql", 500, 470.0)
    assert n == 1
    assert f["check_type"] == "cost_storage_oversized"
    d = f["details"]
    assert d["used_storage_gb"] == 30.0
    assert d["wasted_gb"] == 470.0
    assert d["usage_ratio"] == 0.06
    assert d["suggested_allocation_gb"] >= 20
    assert d["suggested_allocation_gb"] > d["used_storage_gb"]


def test_the_recommendation_never_claims_storage_can_be_shrunk(monkeypatch):
    """The single most important sentence in this check: RDS has no operation that
    reduces AllocatedStorage, so naming ModifyDBInstance (as the scaffold did)
    would send the DBA to a control that does not exist."""
    n, f = _run(monkeypatch, "mysql", 500, 470.0)
    assert n == 1
    r = f["recommendation"]
    assert "줄일 수 없습니다" in r, r
    assert "마이그레이션" in r, r
    assert f["details"]["shrink_supported_by_aws"] is False


def test_gp2_is_offered_as_the_actionable_lever_before_a_migration(monkeypatch):
    """gp2 -> gp3 IS a ModifyDBInstance, so on a gp2 instance it is strictly
    cheaper advice than a migration and belongs in the same finding."""
    n, f = _run(monkeypatch, "mysql", 500, 470.0, storage_type="gp2")
    assert n == 1
    assert "gp3" in f["recommendation"]
    _n, f2 = _run(monkeypatch, "mysql", 500, 470.0, storage_type="gp3")
    assert "gp3로 전환" not in f2["recommendation"]


def test_storage_autoscaling_is_named_because_it_is_how_you_get_here(monkeypatch):
    """Allocation grows automatically under load and never comes back down, so a
    large allocation may be the residue of a past peak rather than a sizing choice."""
    n, f = _run(monkeypatch, "mysql", 500, 470.0, autoscale=1000)
    assert n == 1
    assert "자동 확장" in f["recommendation"]


@pytest.mark.parametrize("allocated,free_gb,why", [
    (60, 20.0, "40 of 60 used = 67%: not proportionally wasteful"),
    (100, 60.0, "40 of 100 used = 40%: above the ratio gate"),
    (40, 38.0, "2 of 40 used = 5%, but only 38GB wasted: under the absolute floor"),
])
def test_waste_must_be_both_proportional_and_absolute(monkeypatch, allocated, free_gb, why):
    """A 40 GB instance at 5% is proportionally extreme and absolutely trivial.
    Both gates have to trip or the Cost tab fills with migrations not worth doing."""
    n, f = _run(monkeypatch, "mysql", allocated, free_gb)
    assert n == 0, (why, f)


def test_the_absolute_floor_boundary_is_inclusive(monkeypatch):
    """Pinned because I got it wrong first: the gate is `wasted < 50 -> silent`, so
    exactly 50 GB wasted DOES report. Stating the boundary stops the next reader
    from re-deriving it from the constant."""
    # wasted == free, since wasted = allocated - used = allocated - (allocated - free).
    n_at, _ = _run(monkeypatch, "mysql", 60, 50.0)     # used 10, wasted exactly 50
    n_below, _ = _run(monkeypatch, "mysql", 60, 49.0)  # used 11, wasted 49
    assert n_at == 1, "exactly at the floor must report"
    assert n_below == 0, "just under the floor must stay silent"


@pytest.mark.parametrize("samples", [0, 50, 99])
def test_a_thin_series_says_nothing(monkeypatch, samples):
    """A storage migration is expensive advice; it must not rest on a handful of
    samples."""
    n, f = _run(monkeypatch, "mysql", 500, 470.0, samples=samples)
    assert n == 0, f


def test_missing_or_unparseable_resource_details_is_silent(monkeypatch):
    """cluster_meta rows exist before the first meta collection, and a provisioned
    cluster once wrote the STRING "None" into these fields."""
    monkeypatch.setattr(cc, "_emit_finding", lambda *a, **k: pytest.fail("emitted"))
    monkeypatch.setattr(cc, "_execute", lambda *a, **k: [{"max_free": 0, "samples": 0}])
    for meta in ({"engine": "mysql", "resource_details": None},
                 {"engine": "mysql", "resource_details": "not json"},
                 {"engine": "mysql", "resource_details": json.dumps({})},
                 {"engine": "mysql",
                  "resource_details": json.dumps({"allocated_storage_gb": "None"})},
                 {"engine": None, "resource_details": json.dumps(
                     {"allocated_storage_gb": 500})},
                 {}):
        assert cc._check_storage_rightsize(None, None, None, None, "c1", meta) == 0


def test_the_collector_actually_selects_the_columns_this_check_reads():
    """The gate that would otherwise make this whole check silently dark: the meta
    query did NOT select `engine` or `resource_details`, so the check saw None for
    both and returned 0 for every cluster in the fleet."""
    src = _C.read_text()
    meta_select = src[src.index("SELECT instance_class, engine_mode"):]
    meta_select = meta_select[:meta_select.index("FROM cluster_meta")]
    assert "engine" in meta_select, meta_select
    assert "resource_details" in meta_select, meta_select


def test_the_new_check_type_is_renderable_by_the_panel():
    """A check_type missing from CHECK_LABELS is filtered out of EVERY tab by the
    panel's tab filter, so the finding would be produced and never shown."""
    panel = (_ROOT / "frontend/src/components/dashboard/maintenance-health-panel.tsx"
             ).read_text()
    assert "cost_storage_oversized:" in panel, "the panel cannot render this finding"


def test_the_free_storage_aggregate_ignores_per_instance_rows():
    """The census guard in tests/unit/test_metric_filters.py demands a mixed-row
    proof for every new cluster-level aggregate, and this repo has shipped that bug
    three times: metric_snapshots holds the SAME metric_type at several
    dimensionalities, so MAX(value) without the strict filter mixes the cluster
    total with per-instance rows and returns a silently wrong number.

    Asserted on the SQL rather than by seeding a database, because what can go
    wrong is the predicate going missing, and that is visible in the text the
    collector sends.
    """
    src = _C.read_text()
    stmt = src[src.index("metric_type = 'free_storage_bytes'"):]
    stmt = stmt[:stmt.index('"""') if '"""' in stmt[:600] else 600]
    assert "dimensions IS NULL OR dimensions::text = '{}'" in stmt, stmt


def test_a_per_instance_row_cannot_inflate_the_free_storage_reading(monkeypatch):
    """The behavioural half: whatever rows the query returns, the check must read
    the cluster-level aggregate the SQL asked for. Driven by handing the collector
    the shape a MIXED result would produce if the filter were dropped (a larger
    per-instance free value) and asserting the finding is computed from what the
    filtered query returns, not from the biggest number available."""
    captured = {}

    def fake_emit(rds, arn, sec, db, cluster_id, check_type, severity, subject,
                  value_str, threshold_str, recommendation, details):
        captured.update(details=details)

    monkeypatch.setattr(cc, "_emit_finding", fake_emit)
    # The filtered query returns ONE row: the cluster-level aggregate.
    monkeypatch.setattr(cc, "_execute",
                        lambda *a, **k: [{"max_free": 470.0 * (1024 ** 3),
                                          "samples": 10000}])
    n = cc._check_storage_rightsize(
        None, None, None, None, "c1", _meta("mysql", 500))
    assert n == 1
    # 30 GB used, i.e. 500 - 470. If a per-instance row (say 495 GB free) had been
    # mixed in and won the MAX, used would have read as 5 GB.
    assert captured["details"]["used_storage_gb"] == 30.0
    assert captured["details"]["free_storage_gb"] == 470.0
