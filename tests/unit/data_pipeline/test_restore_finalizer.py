"""Tests for the restore_finalizer Lambda (phase 3 async second half).

Covers id derivation + the per-cluster state machine: waiting while the
restored cluster is still creating, instance creation once available, and
flag-clearing when the target disappeared.
"""

import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock

_HANDLER_PATH = (
    Path(__file__).resolve().parents[3]
    / "data-pipeline"
    / "restore_finalizer"
    / "handler.py"
)
_spec = importlib.util.spec_from_file_location("restore_finalizer_handler", _HANDLER_PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _rds_mock():
    """A boto3 rds client mock with real exception classes so the handler's
    `except rds.exceptions.*` clauses are catchable."""
    rds = MagicMock()
    rds.exceptions.DBClusterNotFoundFault = type(
        "DBClusterNotFoundFault", (Exception,), {}
    )
    rds.exceptions.DBInstanceAlreadyExistsFault = type(
        "DBInstanceAlreadyExistsFault", (Exception,), {}
    )
    return rds


def test_make_instance_id_is_valid():
    iid = handler._make_instance_id("dbops-dev-sample-samplepg789869c8-caf4ladtqz0i")
    assert re.match(r"^[a-zA-Z][a-zA-Z0-9]*(-[a-zA-Z0-9]+)*$", iid)
    assert len(iid) <= 63
    assert "--" not in iid
    assert not iid.endswith("-")


def test_waits_while_cluster_creating():
    rds = _rds_mock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{"Status": "creating"}]}
    table = MagicMock()
    out = handler._finalize_one(rds, table, {"cluster_id": "restored-1"})
    assert out["result"].startswith("waiting")
    rds.create_db_instance.assert_not_called()
    table.update_item.assert_not_called()


def test_creates_instance_when_available_and_empty():
    rds = _rds_mock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{
            "Status": "available",
            "DBClusterMembers": [],
            "Engine": "aurora-postgresql",
            "DBClusterArn": "arn:aws:rds:ap-northeast-2:123456789012:cluster:restored-1",
            "MasterUserSecret": {"SecretArn": "arn:aws:secretsmanager:...:secret:x"},
            "EngineVersion": "15.4",
        }]
    }
    table = MagicMock()
    out = handler._finalize_one(rds, table, {"cluster_id": "restored-1"})
    assert out["result"] == "finalized"
    call = rds.create_db_instance.call_args.kwargs
    assert call["DBInstanceClass"] == "db.serverless"
    assert call["DBClusterIdentifier"] == "restored-1"
    assert call["Engine"] == "aurora-postgresql"
    # flag cleared + connection coords backfilled
    table.update_item.assert_called_once()


def test_skips_instance_creation_when_members_exist():
    rds = _rds_mock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{
            "Status": "available",
            "DBClusterMembers": [{"DBInstanceIdentifier": "i-1"}],
            "Engine": "aurora-postgresql",
            "DBClusterArn": "arn:aws:rds:ap-northeast-2:123456789012:cluster:restored-1",
            "MasterUserSecret": {"SecretArn": "arn:secret"},
        }]
    }
    table = MagicMock()
    out = handler._finalize_one(rds, table, {"cluster_id": "restored-1"})
    assert out["result"] == "finalized"
    rds.create_db_instance.assert_not_called()
    table.update_item.assert_called_once()


def test_clears_flag_when_cluster_not_found():
    rds = _rds_mock()
    rds.describe_db_clusters.side_effect = rds.exceptions.DBClusterNotFoundFault()
    table = MagicMock()
    out = handler._finalize_one(rds, table, {"cluster_id": "gone-1"})
    assert out["result"] == "not_found"
    # pending flag is cleared so we stop polling a dead cluster
    table.update_item.assert_called_once()


def test_instance_already_exists_is_idempotent():
    rds = _rds_mock()
    rds.describe_db_clusters.return_value = {
        "DBClusters": [{
            "Status": "available",
            "DBClusterMembers": [],
            "Engine": "aurora-postgresql",
            "DBClusterArn": "arn:aws:rds:ap-northeast-2:123456789012:cluster:restored-1",
            "MasterUserSecret": {"SecretArn": "arn:secret"},
        }]
    }
    rds.create_db_instance.side_effect = rds.exceptions.DBInstanceAlreadyExistsFault()
    table = MagicMock()
    out = handler._finalize_one(rds, table, {"cluster_id": "restored-1"})
    assert out["result"] == "finalized"
    table.update_item.assert_called_once()
