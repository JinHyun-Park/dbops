"""Unit tests for the cost-handler RI/SP commitments path (?view=commitments).

Pins: registry→(account,region,role) target derivation, tenancy filtering
(only accounts of VISIBLE clusters; fail-closed on scan error), RI parsing
(end = StartTime + Duration, remaining_days, expiring calc), the coarse
running-vs-RI unused estimate, best-effort CE coverage that nulls on failure,
and the security rule that str(e) is NEVER placed in the response body.

No real AWS calls: boto3.resource / _session_for / _savings_plans_list /
tenancy are all patched with deterministic fakes.
"""

import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = ROOT / "api" / "cost" / "handler.py"

# handler.py does `import tenancy` (a sibling module); push api/cost on
# sys.path so it resolves whether this test runs alone or in the full suite.
_COST_DIR = str(ROOT / "api" / "cost")
if _COST_DIR not in sys.path:
    sys.path.insert(0, _COST_DIR)


def _load():
    spec = importlib.util.spec_from_file_location("cost_handler_commitments", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cost_handler_commitments"] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def mod():
    return _load()


START = date(2026, 5, 1)
END = date(2026, 5, 31)
NOW = datetime.now(timezone.utc)


def _body(resp):
    return json.loads(resp["body"])


def _ri(cls, count, remaining_days, multi_az=False, offering="All Upfront", state="active"):
    """A describe_reserved_db_instances row. StartTime/Duration are chosen so
    end lands `remaining_days` from now (RDS RIs have no explicit end)."""
    dur_days = 365
    start = NOW - timedelta(days=dur_days - remaining_days)
    return {
        "DBInstanceClass": cls,
        "DBInstanceCount": count,
        "MultiAZ": multi_az,
        "OfferingType": offering,
        "ProductDescription": "aurora-postgresql",
        "State": state,
        "StartTime": start,
        "Duration": dur_days * 86400,
    }


def _rds_mock(ris, running_instances):
    rds = MagicMock()
    rds.describe_reserved_db_instances.return_value = {"ReservedDBInstances": ris}
    rds.describe_db_instances.return_value = {
        "DBInstances": [
            {"Engine": "aurora-postgresql", "DBInstanceClass": c} for c in running_instances
        ]
    }
    return rds


class _FakeSession:
    def __init__(self, rds):
        self._rds = rds

    def client(self, _service):
        return self._rds


# ---------------------------------------------------------------------------
# _account_from_arn / _describe_active_ris parsing
# ---------------------------------------------------------------------------


def test_account_from_arn(mod):
    arn = "arn:aws:rds:ap-northeast-2:123456789012:cluster:prod-1"
    assert mod._account_from_arn(arn) == "123456789012"
    assert mod._account_from_arn("") == ""
    assert mod._account_from_arn("garbage") == ""


def test_describe_active_ris_computes_end_and_skips_inactive(mod):
    rds = MagicMock()
    rds.describe_reserved_db_instances.return_value = {
        "ReservedDBInstances": [
            _ri("db.r6g.large", 2, remaining_days=10),
            _ri("db.r6g.xlarge", 1, remaining_days=300, state="retired"),  # skipped
        ]
    }
    rows = mod._describe_active_ris(rds, "111", "ap-northeast-2")
    assert len(rows) == 1
    r = rows[0]
    assert r["instance_class"] == "db.r6g.large"
    assert r["count"] == 2
    assert r["account"] == "111" and r["region"] == "ap-northeast-2"
    # end is an ISO string ~10 days out; remaining_days ~10 (allow off-by-one)
    assert r["end"] is not None
    assert 9 <= r["remaining_days"] <= 10


def test_describe_active_ris_soft_fails_to_empty(mod):
    rds = MagicMock()
    rds.describe_reserved_db_instances.side_effect = RuntimeError("boom")
    assert mod._describe_active_ris(rds, "111", "us-east-1") == []


# ---------------------------------------------------------------------------
# Tenancy: targets restricted to accounts of VISIBLE aurora clusters
# ---------------------------------------------------------------------------


_ITEMS = [
    {"cluster_id": "c-open", "engine": "aurora-postgresql", "region": "ap-northeast-2",
     "cluster_arn": "arn:aws:rds:ap-northeast-2:111111111111:cluster:c-open", "spoke_role_arn": ""},
    {"cluster_id": "c-teamB", "engine": "aurora-mysql", "region": "us-east-1",
     "cluster_arn": "arn:aws:rds:us-east-1:222222222222:cluster:c-teamB",
     "spoke_role_arn": "arn:aws:iam::222222222222:role/spoke"},
    {"cluster_id": "c-ddb", "engine": "dynamodb", "region": "us-east-1", "account_id": "333333333333"},
]


def _patch_registry(mod, monkeypatch, items):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    fake_table = MagicMock()
    fake_table.scan.return_value = {"Items": items}
    fake_res = MagicMock()
    fake_res.Table.return_value = fake_table
    monkeypatch.setattr(mod.boto3, "resource", lambda _svc: fake_res)


def test_targets_viewer_sees_only_visible_account(mod, monkeypatch):
    _patch_registry(mod, monkeypatch, _ITEMS)
    monkeypatch.setattr(mod.tenancy, "visible_cluster_ids", lambda event, items: {"c-open"})
    targets, failed = mod._aurora_commitment_targets({})
    assert failed is False
    accounts = {t["account"] for t in targets}
    assert accounts == {"111111111111"}  # c-teamB not visible, c-ddb not aurora


def test_targets_admin_sees_all_aurora_accounts(mod, monkeypatch):
    _patch_registry(mod, monkeypatch, _ITEMS)
    monkeypatch.setattr(mod.tenancy, "visible_cluster_ids", lambda event, items: None)
    targets, failed = mod._aurora_commitment_targets({})
    assert failed is False
    accounts = {t["account"] for t in targets}
    assert accounts == {"111111111111", "222222222222"}  # both aurora; ddb excluded


def test_targets_fail_closed_on_scan_error(mod, monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters")
    fake_res = MagicMock()
    fake_res.Table.return_value.scan.side_effect = RuntimeError("ddb down")
    monkeypatch.setattr(mod.boto3, "resource", lambda _svc: fake_res)
    targets, failed = mod._aurora_commitment_targets({})
    assert targets == [] and failed is True  # fail CLOSED, surface nothing


# ---------------------------------------------------------------------------
# Full view: summary (total/expiring/unused), coverage null on error, no leak
# ---------------------------------------------------------------------------


def _ce_ok():
    ce = MagicMock()
    ce.get_reservation_coverage.return_value = {
        "Total": {"CoverageHours": {"CoverageHoursPercentage": "62.5"}}
    }
    ce.get_savings_plans_coverage.return_value = {
        "SavingsPlansCoverages": [{"Coverage": {"CoveragePercentage": "0"}}]
    }
    return ce


def test_commitments_view_summary_and_coverage(mod, monkeypatch):
    # 2× db.r6g.large (10d, expiring) + 1× db.r6g.xlarge (300d); running: one
    # of each → unused = (2-1) + (1-1) = 1; expiring_30d = 2; total = 3.
    ris = [_ri("db.r6g.large", 2, 10), _ri("db.r6g.xlarge", 1, 300)]
    rds = _rds_mock(ris, running_instances=["db.r6g.large", "db.r6g.xlarge"])
    monkeypatch.setattr(mod, "_aurora_commitment_targets",
                        lambda event: ([{"account": "111", "region": "ap-northeast-2", "role_arn": ""}], False))
    monkeypatch.setattr(mod, "_session_for", lambda region, role_arn="": _FakeSession(rds))
    monkeypatch.setattr(mod, "_savings_plans_list", lambda: [])

    body = _body(mod._handle_commitments_view(_ce_ok(), START, END, 30, {}))
    assert body["view"] == "commitments"
    assert body["summary"] == {"total": 3, "expiring_30d": 2, "unused_estimate": 1}
    assert body["coverage"] == {"reservation_pct": 62.5, "savings_plans_pct": 0.0}
    # RIs sorted soonest-expiry first.
    assert body["ris"][0]["instance_class"] == "db.r6g.large"
    assert body["savings_plans"] == []


def test_commitments_view_coverage_null_and_no_str_e_leak(mod, monkeypatch):
    secret = "SUPER_SECRET_internal_arn_and_creds"
    ris = [_ri("db.r6g.large", 1, 200)]
    rds = _rds_mock(ris, running_instances=["db.r6g.large"])
    monkeypatch.setattr(mod, "_aurora_commitment_targets",
                        lambda event: ([{"account": "111", "region": "us-east-1", "role_arn": ""}], False))
    monkeypatch.setattr(mod, "_session_for", lambda region, role_arn="": _FakeSession(rds))
    monkeypatch.setattr(mod, "_savings_plans_list", lambda: None)

    ce = MagicMock()
    ce.get_reservation_coverage.side_effect = RuntimeError(secret)
    ce.get_savings_plans_coverage.side_effect = RuntimeError(secret)

    resp = mod._handle_commitments_view(ce, START, END, 30, {})
    body = _body(resp)
    assert body["coverage"] is None  # both CE calls failed → null, not partial
    assert body["savings_plans"] is None
    assert secret not in resp["body"]  # security: str(e) never surfaced


def test_commitments_view_scan_failed_note(mod, monkeypatch):
    monkeypatch.setattr(mod, "_aurora_commitment_targets", lambda event: ([], True))
    monkeypatch.setattr(mod, "_savings_plans_list", lambda: None)
    ce = MagicMock()
    ce.get_reservation_coverage.side_effect = RuntimeError("x")
    ce.get_savings_plans_coverage.side_effect = RuntimeError("x")
    body = _body(mod._handle_commitments_view(ce, START, END, 30, {}))
    assert body["ris"] == []
    assert body["summary"]["total"] == 0
    assert "레지스트리" in body["note"]


def test_lambda_handler_routes_commitments(mod, monkeypatch):
    monkeypatch.setattr(mod, "_aurora_commitment_targets", lambda event: ([], False))
    monkeypatch.setattr(mod, "_savings_plans_list", lambda: None)
    monkeypatch.setattr(mod, "_reservation_coverage", lambda ce, s, e: None)
    monkeypatch.setattr(mod, "boto3", MagicMock())
    event = {"httpMethod": "GET", "queryStringParameters": {"view": "commitments", "days": "30"}}
    body = _body(mod.lambda_handler(event, None))
    assert body["view"] == "commitments"
