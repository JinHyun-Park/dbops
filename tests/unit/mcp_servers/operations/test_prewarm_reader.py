"""Tests for prewarm_reader (P2-④), approval-gated Aurora PG reader prewarm.

Covers: PG-only gate (non-PG → unsupported_engine), preview stage (plan +
NO direct connect), execute stage (mocked rds + Data-API CREATE EXTENSION +
mocked pg_direct → prewarmed with relations_warmed + endpoint exclude→include),
writer-instance rejection, verify_approval FAIL-CLOSED (denied + real
payload-hash mismatch → no connect, no rds writes), and the connect_failed SG
hint (with the reader re-included by the safety net).

pg_direct is patched module-wide so pg8000 is never imported; the mocked rds
uses real return dicts (no bare-MagicMock paginate loops).
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.prewarm_reader import prewarm_reader_impl
from mcp_servers.shared.approval_guard import canonical_action_hash

_PR = "mcp_servers.operations.tools.prewarm_reader"


def _cache(engine="aurora-postgresql"):
    """A CacheClient stand-in: engine lookup for the PG gate, _resolve_target for
    creds, and execute_on_target for the CREATE EXTENSION writer calls."""
    cache = MagicMock()
    cache.execute.return_value.rows = [{"engine": engine}]
    cache._resolve_target.return_value = {
        "cluster_arn": "arn:aws:rds:...:cluster:prod-pg-1",
        "secret_arn": "arn:aws:secretsmanager:...:secret:prod-pg-1",
        "db_name": "appdb",
    }
    return cache


def _rds(members=None, excluded=None):
    """rds client with a reader (reader-1) + writer (writer-1), a resolvable
    reader endpoint, and a custom endpoint carrying `excluded` members."""
    members = members or [
        {"DBInstanceIdentifier": "reader-1", "IsClusterWriter": False},
        {"DBInstanceIdentifier": "writer-1", "IsClusterWriter": True},
    ]
    rds = MagicMock()
    rds.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterMembers": members}]}
    rds.describe_db_instances.return_value = {"DBInstances": [
        {"DBInstanceIdentifier": "reader-1",
         "Endpoint": {"Address": "reader-1.abc.rds.amazonaws.com", "Port": 5432}},
    ]}
    rds.describe_db_cluster_endpoints.return_value = {
        "DBClusterEndpoints": [{"ExcludedMembers": list(excluded or [])}]
    }
    rds.modify_db_cluster_endpoint.return_value = {"Status": "modifying"}
    return rds


def _secrets():
    s = MagicMock()
    s.get_secret_value.return_value = {"SecretString": json.dumps({"username": "u", "password": "p"})}
    return s


def _cfc(rds, secrets):
    """client_for_cluster dispatcher by service."""
    def factory(cluster_id, service):
        if service == "rds":
            return rds
        if service == "secretsmanager":
            return secrets
        raise AssertionError(f"unexpected service {service!r}")
    return factory


def _fake_pg(buffers=(10, 160), blocks=None):
    """A patched pg_direct module: connect() returns a closeable conn, query()
    dispatches on the SQL text to return fake buffercache/relation/prewarm rows."""
    blocks = blocks or {"public.orders": 100, "public.users": 50}
    state = {"buf": list(buffers)}

    def query(conn, sql, params=None):
        if "pg_buffercache" in sql:
            return [{"n": state["buf"].pop(0)}]
        if "pg_relation_size" in sql:
            return [{"rel": "public.orders", "bytes": 4096},
                    {"rel": "public.users", "bytes": 2048}]
        if "pg_prewarm" in sql:
            return [{"blocks": blocks[params["rel"]]}]
        return []

    pg = MagicMock()
    pg.connect.return_value = MagicMock()
    pg.query.side_effect = query
    return pg


# ───────────────────────── engine gate ─────────────────────────

def test_non_pg_is_unsupported():
    """Aurora MySQL (relational but not PG) → unsupported_engine, no connect."""
    with patch(f"{_PR}.pg_direct") as pg:
        out = prewarm_reader_impl(_cache("aurora-mysql"), cluster_id="prod-mysql-1",
                                  reader_instance_id="reader-1")
    assert out["status"] == "unsupported_engine"
    pg.connect.assert_not_called()


# ───────────────────────── preview stage ─────────────────────────

def test_preview_returns_plan_without_connecting():
    with patch(f"{_PR}.pg_direct") as pg, patch(f"{_PR}.client_for_cluster") as cfc:
        out = prewarm_reader_impl(_cache(), cluster_id="prod-pg-1",
                                  reader_instance_id="reader-1",
                                  endpoint_identifier="ep-ro", top_n=20)
    assert out["status"] == "approval_required"
    assert "pg_prewarm" in out["cli_preview"]
    assert "리더 인스턴스 reader-1" in out["cli_preview"]
    pg.connect.assert_not_called()
    cfc.assert_not_called()  # no rds resolution before approval


# ───────────────────────── execute stage ─────────────────────────

@patch(f"{_PR}.verify_approval", return_value={"ok": True})
def test_execute_prewarms_with_endpoint_choreography(_guard):
    cache = _cache()
    rds = _rds()
    pg = _fake_pg()
    with patch(f"{_PR}.pg_direct", pg), \
         patch(f"{_PR}.client_for_cluster", _cfc(rds, _secrets())):
        out = prewarm_reader_impl(cache, cluster_id="prod-pg-1",
                                  reader_instance_id="reader-1",
                                  endpoint_identifier="ep-ro", top_n=20,
                                  approved=True, approval_id="aid-1")
    assert out["status"] == "prewarmed"
    assert out["relations_warmed"] == [
        {"rel": "public.orders", "blocks": 100},
        {"rel": "public.users", "blocks": 50},
    ]
    assert out["total_blocks"] == 150
    assert out["buffers_before"] == 10 and out["buffers_after"] == 160
    assert out["endpoint_choreography"] == "excluded→included"
    # CREATE EXTENSION ran on the writer (Data API) for both extensions.
    assert cache.execute_on_target.call_count == 2
    # exclude then include: modify called twice, first excludes the reader.
    assert rds.modify_db_cluster_endpoint.call_count == 2
    first = rds.modify_db_cluster_endpoint.call_args_list[0].kwargs
    assert first["ExcludedMembers"] == ["reader-1"]


@patch(f"{_PR}.verify_approval", return_value={"ok": True})
def test_execute_without_endpoint_skips_choreography(_guard):
    rds = _rds()
    with patch(f"{_PR}.pg_direct", _fake_pg()), \
         patch(f"{_PR}.client_for_cluster", _cfc(rds, _secrets())):
        out = prewarm_reader_impl(_cache(), cluster_id="prod-pg-1",
                                  reader_instance_id="reader-1",
                                  approved=True, approval_id="aid-1")
    assert out["status"] == "prewarmed"
    assert out["endpoint_choreography"] == "skipped"
    rds.modify_db_cluster_endpoint.assert_not_called()


@patch(f"{_PR}.verify_approval", return_value={"ok": True})
def test_writer_instance_rejected(_guard):
    rds = _rds()
    pg = _fake_pg()
    with patch(f"{_PR}.pg_direct", pg), \
         patch(f"{_PR}.client_for_cluster", _cfc(rds, _secrets())):
        out = prewarm_reader_impl(_cache(), cluster_id="prod-pg-1",
                                  reader_instance_id="writer-1",
                                  endpoint_identifier="ep-ro",
                                  approved=True, approval_id="aid-1")
    assert out["status"] == "not_a_reader"
    rds.modify_db_cluster_endpoint.assert_not_called()  # no exclude on a writer
    pg.connect.assert_not_called()


# ───────────────────────── approval FAIL-CLOSED ─────────────────────────

def test_denied_approval_never_connects():
    """A guard denial refuses before any rds resolution or direct connect."""
    with patch(f"{_PR}.verify_approval", return_value={"ok": False, "reason": "nope"}), \
         patch(f"{_PR}.pg_direct") as pg, patch(f"{_PR}.client_for_cluster") as cfc:
        out = prewarm_reader_impl(_cache(), cluster_id="prod-pg-1",
                                  reader_instance_id="reader-1",
                                  approved=True, approval_id="aid-1")
    assert out["status"] == "approval_denied"
    pg.connect.assert_not_called()
    cfc.assert_not_called()


def test_payload_hash_mismatch_rejected():
    """A real approval minted for top_n=20 cannot be consumed for top_n=99 —
    the guard's payload_hash refuses it and nothing is executed."""
    row = {
        "approval_id": "aid-1", "created_at": "1", "approval_status": "approved",
        "cluster_id": "prod-pg-1", "action_type": "prewarm_reader",
        "payload_hash": canonical_action_hash("prewarm_reader", {
            "cluster_id": "prod-pg-1", "reader_instance_id": "reader-1",
            "endpoint_identifier": "", "top_n": 20,
        }),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    table = MagicMock()
    table.scan.return_value = {"Items": [row]}
    resource = MagicMock()
    resource.Table.return_value = table
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}, clear=False), \
         patch("mcp_servers.shared.approval_guard.boto3.resource", return_value=resource), \
         patch(f"{_PR}.pg_direct") as pg, patch(f"{_PR}.client_for_cluster") as cfc:
        out = prewarm_reader_impl(_cache(), cluster_id="prod-pg-1",
                                  reader_instance_id="reader-1", top_n=99,
                                  approved=True, approval_id="aid-1")
    assert out["status"] == "approval_denied"
    assert "match" in out["reason"].lower()
    table.update_item.assert_not_called()  # never consumed
    pg.connect.assert_not_called()
    cfc.assert_not_called()


# ───────────────────────── connect_failed ─────────────────────────

@patch(f"{_PR}.verify_approval", return_value={"ok": True})
def test_connect_failed_returns_sg_hint_and_reincludes(_guard):
    rds = _rds()
    pg = _fake_pg()
    pg.connect.side_effect = Exception("connection refused")
    with patch(f"{_PR}.pg_direct", pg), \
         patch(f"{_PR}.client_for_cluster", _cfc(rds, _secrets())):
        out = prewarm_reader_impl(_cache(), cluster_id="prod-pg-1",
                                  reader_instance_id="reader-1",
                                  endpoint_identifier="ep-ro",
                                  approved=True, approval_id="aid-1")
    assert out["status"] == "connect_failed"
    assert "5432" in out["hint"]
    # excluded then re-included by the finally safety net → 2 modify calls.
    assert rds.modify_db_cluster_endpoint.call_count == 2
