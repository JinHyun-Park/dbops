"""Tests for the operations-handler FAIL-CLOSED engine gate (review fix #3).

The 3 DynamoDB write tools must REFUSE when the engine family cannot be resolved
(None → unsupported_engine) or when the cluster is the wrong engine (Aurora). A
valid-looking approval must NEVER drive a write at the wrong/unknown engine — so
we assert the AWS write method is never even reached (the impl is monkeypatched to
a spy that fails the test if called)."""

import json
import os
from unittest.mock import MagicMock, patch

# The operations handler instantiates CacheClient() at import time, which reads
# CACHE_DB_CLUSTER_ARN. Set a dummy before import so this file collects in
# isolation too (mirrors simulation/test_engine_guard.py). The cache is never
# actually queried — _resolve_family is monkeypatched in every test.
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

import mcp_servers.operations.handler as handler  # noqa: E402


class _Ctx:
    def __init__(self, tool_name):
        self.client_context = MagicMock()
        self.client_context.custom = {"bedrockAgentCoreToolName": f"x___{tool_name}"}


def _invoke(tool_name, event):
    result = handler.lambda_handler(event, _Ctx(tool_name))
    return json.loads(result["content"][0]["text"])


def _patch_family(fam):
    return patch.object(handler, "_resolve_family", lambda cid: fam)


def test_gate_none_family_fail_closed():
    """fix #3: family=None (unresolvable cluster) → unsupported_engine, impl
    never invoked — even with approved=true + an approval_id present."""
    spy = MagicMock()
    with _patch_family(None), patch.dict(
        handler.TOOLS["modify_dynamodb_capacity"], {"impl": spy}
    ):
        result = _invoke(
            "modify_dynamodb_capacity",
            {"cluster_id": "ghost", "rcu": 5, "wcu": 5, "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    assert "could not be resolved" in result["reason"]
    spy.assert_not_called()


def test_gate_wrong_engine_aurora_refused():
    """A DynamoDB tool called on an Aurora (relational) cluster → refused."""
    spy = MagicMock()
    with _patch_family("relational"), patch.dict(
        handler.TOOLS["enable_dynamodb_pitr"], {"impl": spy}
    ):
        result = _invoke(
            "enable_dynamodb_pitr",
            {"cluster_id": "prod-pg-1", "enabled": True, "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    spy.assert_not_called()


def test_gate_dynamodb_family_passes_to_impl():
    """A resolved dynamodb family passes the gate and the impl runs."""
    spy = MagicMock(return_value={"status": "approval_required"})
    with _patch_family("dynamodb"), patch.dict(
        handler.TOOLS["modify_dynamodb_ttl"], {"impl": spy}
    ):
        result = _invoke(
            "modify_dynamodb_ttl",
            {"cluster_id": "ddb-abc", "attribute": "expires_at"},
        )
    assert result["status"] == "approval_required"
    spy.assert_called_once()


def test_gate_aurora_tool_ungated():
    """The Aurora tools stay UNGATED — execute_sql is not in the gate map, so a
    relational cluster reaches the impl without an engine check."""
    spy = MagicMock(return_value={"status": "ok"})
    with _patch_family("relational"), patch.dict(
        handler.TOOLS["execute_sql"], {"impl": spy}
    ):
        result = _invoke("execute_sql", {"cluster_id": "prod-pg-1", "sql": "SELECT 1"})
    assert result["status"] == "ok"
    spy.assert_called_once()


# ===== Aurora CLUSTER parameter group: cluster_parameter gate (E-0) =====


def test_gate_modify_parameter_rds_instance_refused():
    """modify_parameter on a standalone RDS instance → unsupported_engine. Those
    engines have INSTANCE parameter groups, so describe_db_clusters would fail
    (and used to leak the raw AWS exception into the response)."""
    spy = MagicMock()
    with _patch_family("rds_instance"), patch.dict(
        handler.TOOLS["modify_parameter"], {"impl": spy}
    ):
        result = _invoke(
            "modify_parameter",
            {"cluster_id": "rds-mysql-1", "parameter_name": "max_connections",
             "value": "200", "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    spy.assert_not_called()


def test_gate_modify_parameter_nonrelational_refused():
    """documentdb / dynamodb / elasticache all lack cluster_parameter."""
    for fam in ("documentdb", "dynamodb", "elasticache"):
        spy = MagicMock()
        with _patch_family(fam), patch.dict(
            handler.TOOLS["modify_parameter"], {"impl": spy}
        ):
            result = _invoke(
                "modify_parameter",
                {"cluster_id": "x-1", "parameter_name": "p", "value": "1"},
            )
        assert result["status"] == "unsupported_engine", fam
        spy.assert_not_called()


def test_gate_modify_parameter_none_family_fail_closed():
    spy = MagicMock()
    with _patch_family(None), patch.dict(
        handler.TOOLS["modify_parameter"], {"impl": spy}
    ):
        result = _invoke(
            "modify_parameter",
            {"cluster_id": "ghost", "parameter_name": "p", "value": "1",
             "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    assert "could not be resolved" in result["reason"]
    spy.assert_not_called()


def test_gate_modify_parameter_relational_passes_to_impl():
    spy = MagicMock(return_value={"status": "approval_required"})
    with _patch_family("relational"), patch.dict(
        handler.TOOLS["modify_parameter"], {"impl": spy}
    ):
        result = _invoke(
            "modify_parameter",
            {"cluster_id": "prod-pg-1", "parameter_name": "work_mem", "value": "64MB"},
        )
    assert result["status"] == "approval_required"
    spy.assert_called_once()


# ===== DocDB write tools: same FAIL-CLOSED gate (docdb_write) =====


def test_gate_docdb_none_family_fail_closed():
    """fix #3: set_docdb_profiler on an unresolvable cluster → unsupported_engine,
    impl never invoked — so no Mongo connect even with a valid-looking approval."""
    spy = MagicMock()
    with _patch_family(None), patch.dict(
        handler.TOOLS["set_docdb_profiler"], {"impl": spy}
    ):
        result = _invoke(
            "set_docdb_profiler",
            {"cluster_id": "ghost", "level": 1, "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    assert "could not be resolved" in result["reason"]
    spy.assert_not_called()


def test_gate_docdb_wrong_engine_dynamodb_refused():
    """create_docdb_index on a DynamoDB cluster (no docdb_write cap) → refused."""
    spy = MagicMock()
    with _patch_family("dynamodb"), patch.dict(
        handler.TOOLS["create_docdb_index"], {"impl": spy}
    ):
        result = _invoke(
            "create_docdb_index",
            {"cluster_id": "ddb-abc", "db": "app", "collection": "c",
             "keys": [["a", 1]], "name": "ix", "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    spy.assert_not_called()


def test_gate_docdb_family_passes_to_impl():
    """A resolved documentdb family passes the gate and the impl runs."""
    spy = MagicMock(return_value={"status": "approval_required"})
    with _patch_family("documentdb"), patch.dict(
        handler.TOOLS["set_docdb_profiler"], {"impl": spy}
    ):
        # `enabled`, not `level`. The argument guard rejects out-of-schema keys, and
        # this payload carried an invented one, so the test was asserting the gate
        # while actually exercising nothing.
        result = _invoke("set_docdb_profiler", {"cluster_id": "docdb-1", "enabled": True})
    assert result["status"] == "approval_required"
    spy.assert_called_once()


# ===== ElastiCache live_read gate =====


def test_gate_elasticache_none_family_fail_closed():
    """elasticache_live_read on an unresolvable cluster → unsupported_engine,
    impl never invoked."""
    spy = MagicMock()
    with _patch_family(None), patch.dict(
        handler.TOOLS["elasticache_live_read"], {"impl": spy}
    ):
        result = _invoke(
            "elasticache_live_read",
            {"cluster_id": "ghost-ec"},
        )
    assert result["status"] == "unsupported_engine"
    assert "could not be resolved" in result["reason"]
    spy.assert_not_called()


def test_gate_elasticache_wrong_engine_relational_refused():
    """elasticache_live_read on a relational (Aurora) cluster → refused."""
    spy = MagicMock()
    with _patch_family("relational"), patch.dict(
        handler.TOOLS["elasticache_live_read"], {"impl": spy}
    ):
        result = _invoke(
            "elasticache_live_read",
            {"cluster_id": "prod-pg-1"},
        )
    assert result["status"] == "unsupported_engine"
    spy.assert_not_called()


def test_gate_elasticache_family_passes_to_impl():
    """A resolved elasticache family passes the live_read gate and the impl runs."""
    spy = MagicMock(return_value={"status": "ok", "engine": "redis"})
    with _patch_family("elasticache"), patch.dict(
        handler.TOOLS["elasticache_live_read"], {"impl": spy}
    ):
        result = _invoke("elasticache_live_read", {"cluster_id": "ec-redis-1"})
    assert result["status"] == "ok"
    spy.assert_called_once()


# ===== Aurora custom-endpoint tools — RELATIONAL-only positive gate (P2-⑤) =====


def test_gate_custom_endpoint_wrong_engine_refused():
    """create_custom_endpoint on a DynamoDB cluster (no custom_endpoint cap) →
    unsupported_engine, impl never invoked even with a valid-looking approval."""
    spy = MagicMock()
    with _patch_family("dynamodb"), patch.dict(
        handler.TOOLS["create_custom_endpoint"], {"impl": spy}
    ):
        result = _invoke(
            "create_custom_endpoint",
            {"cluster_id": "ddb-abc", "endpoint_identifier": "ep-x",
             "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    spy.assert_not_called()


def test_gate_custom_endpoint_none_family_fail_closed():
    """delete_custom_endpoint on an unresolvable cluster → unsupported_engine."""
    spy = MagicMock()
    with _patch_family(None), patch.dict(
        handler.TOOLS["delete_custom_endpoint"], {"impl": spy}
    ):
        result = _invoke(
            "delete_custom_endpoint",
            {"cluster_id": "ghost", "endpoint_identifier": "ep-x"},
        )
    assert result["status"] == "unsupported_engine"
    assert "could not be resolved" in result["reason"]
    spy.assert_not_called()


def test_gate_custom_endpoint_relational_passes_to_impl():
    """A resolved relational family passes the gate and the impl runs."""
    spy = MagicMock(return_value={"status": "approval_required"})
    with _patch_family("relational"), patch.dict(
        handler.TOOLS["modify_custom_endpoint"], {"impl": spy}
    ):
        result = _invoke(
            "modify_custom_endpoint",
            {"cluster_id": "prod-pg-1", "endpoint_identifier": "ep-x", "static_members": ["i-1"]},
        )
    assert result["status"] == "approval_required"
    spy.assert_called_once()


# ===== Standalone RDS instance write tools — rds_instance-only positive gate (R-3) =====


def test_gate_instance_write_none_family_fail_closed():
    """reboot_rds_instance on an unresolvable cluster → unsupported_engine, impl
    never invoked even with a valid-looking approval."""
    spy = MagicMock()
    with _patch_family(None), patch.dict(
        handler.TOOLS["reboot_rds_instance"], {"impl": spy}
    ):
        result = _invoke(
            "reboot_rds_instance",
            {"cluster_id": "ghost", "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    assert "could not be resolved" in result["reason"]
    spy.assert_not_called()


def test_gate_instance_write_aurora_refused():
    """A standalone-instance tool on an Aurora (relational) cluster → refused;
    Aurora has no instance_write capability."""
    spy = MagicMock()
    with _patch_family("relational"), patch.dict(
        handler.TOOLS["create_rds_snapshot"], {"impl": spy}
    ):
        result = _invoke(
            "create_rds_snapshot",
            {"cluster_id": "prod-pg-1", "approved": True, "approval_id": "x"},
        )
    assert result["status"] == "unsupported_engine"
    spy.assert_not_called()


def test_gate_instance_write_rds_instance_passes_to_impl():
    """A resolved rds_instance family passes the gate and the impl runs."""
    spy = MagicMock(return_value={"status": "approval_required"})
    with _patch_family("rds_instance"), patch.dict(
        handler.TOOLS["modify_rds_instance_class"], {"impl": spy}
    ):
        result = _invoke(
            "modify_rds_instance_class",
            {"cluster_id": "rds-mysql-1", "target_class": "db.r6g.large"},
        )
    assert result["status"] == "approval_required"
    spy.assert_called_once()
