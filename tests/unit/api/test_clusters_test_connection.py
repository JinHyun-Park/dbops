"""Tests for /api/clusters/test-connection — pre-flight verification
that runs STS AssumeRole + DescribeDBClusters + master_user_secret
checks without persisting anything."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_CLUSTERS_DIR = Path(__file__).resolve().parents[3] / "api" / "clusters"
# clusters/handler.py does `import seeder` — its sibling. Push the
# directory onto sys.path so the import resolves before exec.
sys.path.insert(0, str(_CLUSTERS_DIR))

_PATH = _CLUSTERS_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("clusters_handler", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _event(body):
    return {
        "httpMethod": "POST",
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/api/clusters/test-connection",
        "body": json.dumps(body),
        "headers": {},
    }


# lambda_handler reads CLUSTERS_TABLE before branching; even routes that
# don't touch DDB need the env to be present at module entry. Patch
# everywhere via an autouse fixture.
import pytest


@pytest.fixture(autouse=True)
def _clusters_table_env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


def test_missing_cluster_id_400():
    res = handler.lambda_handler(_event({"region": "ap-northeast-2"}), None)
    assert res["statusCode"] == 400
    assert "cluster_id" in json.loads(res["body"])["error"]


def test_missing_region_400():
    res = handler.lambda_handler(_event({"cluster_id": "x"}), None)
    assert res["statusCode"] == 400


def test_invalid_json_400():
    e = {
        "httpMethod": "POST",
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/api/clusters/test-connection",
        "body": "not json",
        "headers": {},
    }
    res = handler.lambda_handler(e, None)
    assert res["statusCode"] == 400


@patch.object(handler, "_session_for")
def test_same_account_skips_assume_role(mock_session):
    """Same-account (no spoke_role_arn) should record assume_role as
    skipped, not run STS."""
    mock_sess = MagicMock()
    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [
            {
                "Engine": "aurora-postgresql",
                "EngineVersion": "15.10",
                "Endpoint": "x.aurora.example",
                "MasterUserSecret": {"SecretArn": "arn:secret"},
            }
        ]
    }
    mock_sess.client.return_value = mock_rds
    mock_session.return_value = mock_sess

    res = handler.lambda_handler(
        _event({"cluster_id": "prod-pg-1", "region": "ap-northeast-2"}),
        None,
    )
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert body["ok"] is True
    steps_by_name = {s["name"]: s for s in body["steps"]}
    assert steps_by_name["assume_role"]["status"] == "skipped"
    assert steps_by_name["describe_cluster"]["status"] == "ok"
    assert steps_by_name["master_user_secret"]["status"] == "ok"


@patch.object(handler, "_session_for")
def test_cross_account_runs_assume_role(mock_session):
    """spoke_role_arn present → assume_role becomes a real step."""
    mock_sess = MagicMock()
    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [{"Engine": "aurora-mysql", "EngineVersion": "8.0", "Endpoint": "y"}]
    }
    mock_sess.client.return_value = mock_rds
    mock_session.return_value = mock_sess

    res = handler.lambda_handler(
        _event(
            {
                "cluster_id": "prod-mysql",
                "region": "us-east-1",
                "spoke_role_arn": "arn:aws:iam::222222222222:role/dbops-spoke",
            }
        ),
        None,
    )
    body = json.loads(res["body"])
    steps_by_name = {s["name"]: s for s in body["steps"]}
    assert steps_by_name["assume_role"]["status"] == "ok"
    # Master user secret missing → warning (not failure)
    assert steps_by_name["master_user_secret"]["status"] == "warning"


@patch.object(handler, "_session_for")
def test_assume_role_failure_short_circuits(mock_session):
    """If STS fails, describe_cluster + master_user_secret must NOT run."""
    mock_session.side_effect = RuntimeError("AccessDenied")

    res = handler.lambda_handler(
        _event(
            {
                "cluster_id": "prod-pg-1",
                "region": "ap-northeast-2",
                "spoke_role_arn": "arn:aws:iam::222:role/no-trust",
            }
        ),
        None,
    )
    body = json.loads(res["body"])
    assert body["ok"] is False
    step_names = [s["name"] for s in body["steps"]]
    # Only assume_role attempted; the rest are absent.
    assert "assume_role" in step_names
    assert "describe_cluster" not in step_names


@patch.object(handler, "_session_for")
def test_cluster_not_found_returns_failed_step(mock_session):
    """Empty DBClusters list → cluster not found in this account/region."""
    mock_sess = MagicMock()
    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = {"DBClusters": []}
    mock_sess.client.return_value = mock_rds
    mock_session.return_value = mock_sess

    res = handler.lambda_handler(
        _event({"cluster_id": "no-such", "region": "ap-northeast-2"}),
        None,
    )
    body = json.loads(res["body"])
    assert body["ok"] is False
    step = next(s for s in body["steps"] if s["name"] == "describe_cluster")
    assert step["status"] == "failed"
    assert "not found" in step["error"]


@patch.object(handler, "_session_for")
def test_master_user_secret_warning_when_missing(mock_session):
    """Cluster without MasterUserSecret → ok pre-flight but warning step
    so the UI flags the Data API limitation."""
    mock_sess = MagicMock()
    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [{"Engine": "aurora-postgresql", "Endpoint": "x"}]
    }
    mock_sess.client.return_value = mock_rds
    mock_session.return_value = mock_sess

    res = handler.lambda_handler(
        _event({"cluster_id": "prod-pg-1", "region": "ap-northeast-2"}),
        None,
    )
    body = json.loads(res["body"])
    assert body["ok"] is True  # overall still pass
    msg_step = next(s for s in body["steps"] if s["name"] == "master_user_secret")
    assert msg_step["status"] == "warning"
    assert "Secrets Manager" in msg_step["note"]
