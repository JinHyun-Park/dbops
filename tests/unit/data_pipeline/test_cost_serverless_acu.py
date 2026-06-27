"""Serverless v2 ACU rightsizing now uses the OBSERVED serverless_acu metric
(not a CPU proxy). These assert the threshold logic against the configured
min/max ceiling and the skip-without-history guard."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors/cost_check.py"
_spec = importlib.util.spec_from_file_location("cost_check", _C)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)


def _finding_types(mock):
    # _emit_finding(rds, arn, secret, db, cluster_id, finding_type, severity, ...)
    return [call.args[5] for call in mock.call_args_list]


def test_sv2_max_too_high_fires_on_low_observed_acu():
    cc._emit_finding = MagicMock()
    meta = {"serverlessv2_min_acu": 2, "serverlessv2_max_acu": 16}
    acu = {"avg": 3.0, "p95": 4.0, "max": 6.0}  # p95 4 < 16*0.6=9.6 → ceiling overprovisioned
    n = cc._check_serverless_v2_acu(None, None, None, None, "c1", meta, acu)
    assert n == 1
    assert "cost_serverless_max_too_high" in _finding_types(cc._emit_finding)


def test_sv2_skips_without_acu_history():
    """No serverless_acu samples → skip rather than guess (the whole point of
    dropping the CPU proxy)."""
    cc._emit_finding = MagicMock()
    meta = {"serverlessv2_min_acu": 2, "serverlessv2_max_acu": 16}
    n = cc._check_serverless_v2_acu(None, None, None, None, "c1", meta, None)
    assert n == 0
    cc._emit_finding.assert_not_called()


def test_sv2_no_finding_when_well_sized():
    cc._emit_finding = MagicMock()
    meta = {"serverlessv2_min_acu": 8, "serverlessv2_max_acu": 16}
    acu = {"avg": 12.0, "p95": 14.0, "max": 16.0}  # p95 near ceiling, min not low → nothing
    n = cc._check_serverless_v2_acu(None, None, None, None, "c1", meta, acu)
    assert n == 0


def test_sv2_skips_non_serverless():
    cc._emit_finding = MagicMock()
    n = cc._check_serverless_v2_acu(None, None, None, None, "c1", {}, {"p95": 1, "max": 1, "avg": 1})
    assert n == 0
