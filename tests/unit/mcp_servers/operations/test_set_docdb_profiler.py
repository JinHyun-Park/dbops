"""Tests for set_docdb_profiler: the DocumentDB CONTROL-PLANE profiler write.

Managed DocumentDB has no Mongo-protocol profiler command, so the tool drives
the cluster parameter group + the profiler CloudWatch Logs export via boto3. The
tests patch `client_for_cluster` so no AWS is touched, and assert that NO Mongo
command is ever issued (the fake client only exposes the docdb control-plane
methods, so a `command`/`__getitem__` call raises AttributeError).

Covered: 3-state flow (approval_required / approval_denied / modified), default.*
parameter group refused, validation (no AWS call), log-export enable/disable +
skip-when-already-correct, partial failure, never-raise, and no raw exception
text in any response field. The FAIL-CLOSED engine gate lives in the handler
(see test_operations_engine_gate.py)."""

from unittest.mock import MagicMock, patch

import mcp_servers.operations.tools.set_docdb_profiler as mod
from mcp_servers.operations.tools.set_docdb_profiler import set_docdb_profiler_impl

_CUSTOM_PG = "docdb-custom-pg"


class _FakeDocDB:
    """Only the three control-plane calls exist. Any Mongo access (client[db],
    client.command) raises AttributeError/TypeError and fails the test."""

    def __init__(self, pg=_CUSTOM_PG, log_exports=(), raise_on=""):
        self._pg = pg
        self._log_exports = list(log_exports)
        self._raise_on = raise_on
        self.modify_pg_calls = []
        self.modify_cluster_calls = []

    def describe_db_clusters(self, **kw):
        if self._raise_on == "describe":
            raise RuntimeError("describe boom: arn:aws:rds:secret-ish-detail")
        return {"DBClusters": [{
            "DBClusterIdentifier": kw.get("DBClusterIdentifier"),
            "DBClusterParameterGroup": self._pg,
            "EnabledCloudwatchLogsExports": self._log_exports,
        }]}

    def modify_db_cluster_parameter_group(self, **kw):
        if self._raise_on == "modify_pg":
            raise RuntimeError("modify pg boom: internal-detail-42")
        self.modify_pg_calls.append(kw)
        return {}

    def modify_db_cluster(self, **kw):
        if self._raise_on == "modify_cluster":
            raise RuntimeError("modify cluster boom: internal-detail-42")
        self.modify_cluster_calls.append(kw)
        return {}


def _with_client(client):
    return patch.object(mod, "client_for_cluster", lambda cid, service: client)


def _params(client):
    """{ParameterName: ParameterValue} of the single parameter-group write."""
    assert len(client.modify_pg_calls) == 1
    return {
        p["ParameterName"]: p["ParameterValue"]
        for p in client.modify_pg_calls[0]["Parameters"]
    }


def _no_exception_text(result):
    """No response value may carry raw exception text (project hard rule)."""
    blob = " ".join(str(v) for v in result.values())
    for leak in ("boom", "Traceback", "RuntimeError", "internal-detail-42"):
        assert leak not in blob, f"raw exception text leaked into response: {result}"


# ===== validation (before any AWS call) =====


def test_threshold_below_50_rejected_no_aws():
    factory = MagicMock(side_effect=AssertionError("must not call AWS"))
    with patch.object(mod, "client_for_cluster", factory):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, threshold_ms=10
        )
    assert result["status"] == "error"
    assert "threshold_ms" in result["reason"]
    factory.assert_not_called()


def test_sampling_rate_out_of_range_rejected_no_aws():
    factory = MagicMock(side_effect=AssertionError("must not call AWS"))
    with patch.object(mod, "client_for_cluster", factory):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, sampling_rate=1.5
        )
    assert result["status"] == "error"
    assert "sampling_rate" in result["reason"]
    factory.assert_not_called()


# ===== default parameter group refused =====


def test_default_parameter_group_refused():
    """A cluster on default.docdb5.0 cannot be modified at all → refuse with the
    reason stated, and never call a modify API."""
    client = _FakeDocDB(pg="default.docdb5.0")
    with _with_client(client):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, approved=True,
            approval_id="x",
        )
    assert result["status"] == "default_group_refused"
    assert result["parameter_group"] == "default.docdb5.0"
    assert "기본 그룹은 수정할 수 없어" in result["reason"]
    assert client.modify_pg_calls == [] and client.modify_cluster_calls == []


def test_default_group_refused_before_approval_consume():
    """The refusal happens BEFORE verify_approval, so a valid approval is not
    burned on an impossible change."""
    client = _FakeDocDB(pg="default.docdb5.0")
    guard = MagicMock(return_value={"ok": True})
    with _with_client(client), patch.object(mod, "verify_approval", guard):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", approved=True, approval_id="x"
        )
    assert result["status"] == "default_group_refused"
    guard.assert_not_called()


# ===== 3-state flow =====


def test_requires_approval_no_write():
    client = _FakeDocDB()
    with _with_client(client):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, threshold_ms=500,
            sampling_rate=0.5,
        )
    assert result["status"] == "approval_required"
    assert result["parameter_group"] == _CUSTOM_PG
    assert result["threshold_ms"] == 500 and result["sampling_rate"] == 0.5
    assert client.modify_pg_calls == [] and client.modify_cluster_calls == []


def test_approval_denied_when_guard_rejects():
    client = _FakeDocDB()
    with _with_client(client), patch.object(
        mod, "verify_approval", lambda *a, **k: {"ok": False, "reason": "nope"}
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", approved=True, approval_id="x"
        )
    assert result["status"] == "approval_denied"
    assert client.modify_pg_calls == [] and client.modify_cluster_calls == []


def test_approved_enable_writes_parameter_group_and_log_export():
    """Approved enable → the three profiler parameters go into the CUSTOM group
    with ApplyMethod=immediate, and the profiler log export is turned on."""
    client = _FakeDocDB(log_exports=[])
    guard = MagicMock(return_value={"ok": True})
    with _with_client(client), patch.object(mod, "verify_approval", guard):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, threshold_ms=500,
            sampling_rate=0.5, approved=True, approval_id="appr-1",
        )
    assert result["status"] == "modified"
    assert result["profiler"] == "enabled"
    assert result["log_export"] is True
    assert result["log_group"] == "/aws/docdb/profiler"
    assert _params(client) == {
        "profiler": "enabled",
        "profiler_threshold_ms": "500",
        "profiler_sampling_rate": "0.5",
    }
    assert client.modify_pg_calls[0]["DBClusterParameterGroupName"] == _CUSTOM_PG
    assert all(
        p["ApplyMethod"] == "immediate" for p in client.modify_pg_calls[0]["Parameters"]
    )
    assert client.modify_cluster_calls == [{
        "DBClusterIdentifier": "docdb-1",
        "CloudwatchLogsExportConfiguration": {"EnableLogTypes": ["profiler"]},
    }]


def test_approval_consumed_exactly_once_with_payload_binding():
    """verify_approval is called ONCE with the action name + the effective
    {enabled, threshold_ms, sampling_rate} payload (hash binding unchanged)."""
    client = _FakeDocDB()
    guard = MagicMock(return_value={"ok": True})
    with _with_client(client), patch.object(mod, "verify_approval", guard):
        set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, threshold_ms=200,
            sampling_rate=0.25, approved=True, approval_id="appr-1",
        )
    guard.assert_called_once_with(
        "appr-1", "docdb-1", "set_docdb_profiler",
        payload={"enabled": True, "threshold_ms": 200, "sampling_rate": 0.25},
    )


def test_disable_turns_off_parameter_and_log_export():
    client = _FakeDocDB(log_exports=["profiler", "audit"])
    with _with_client(client), patch.object(
        mod, "verify_approval", lambda *a, **k: {"ok": True}
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=False, approved=True,
            approval_id="x",
        )
    assert result["status"] == "modified"
    assert result["profiler"] == "disabled" and result["log_export"] is False
    # Only the profiler switch is written when disabling, and the response must
    # not claim a threshold/sampling value it never wrote.
    assert _params(client) == {"profiler": "disabled"}
    assert "threshold_ms" not in result and "sampling_rate" not in result
    assert client.modify_cluster_calls[0]["CloudwatchLogsExportConfiguration"] == {
        "DisableLogTypes": ["profiler"]
    }


def test_log_export_already_enabled_is_not_re_enabled():
    """Re-enabling an already-exported log type is an API error, so skip it."""
    client = _FakeDocDB(log_exports=["profiler"])
    with _with_client(client), patch.object(
        mod, "verify_approval", lambda *a, **k: {"ok": True}
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, approved=True,
            approval_id="x",
        )
    assert result["status"] == "modified" and result["log_export"] is True
    assert client.modify_cluster_calls == []


def test_string_false_disables():
    """A gateway-supplied "false" must not be truthy: bool("false") is True."""
    client = _FakeDocDB()
    with _with_client(client), patch.object(
        mod, "verify_approval", lambda *a, **k: {"ok": True}
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled="false", approved=True,
            approval_id="x",
        )
    assert result["profiler"] == "disabled"


# ===== never-raise + no exception text in the response =====


def test_describe_failure_is_lookup_failed_without_exception_text():
    client = _FakeDocDB(raise_on="describe")
    with _with_client(client):
        result = set_docdb_profiler_impl(MagicMock(), cluster_id="docdb-1")
    assert result["status"] == "lookup_failed"
    _no_exception_text(result)


def test_parameter_group_write_failure_without_exception_text():
    client = _FakeDocDB(raise_on="modify_pg")
    with _with_client(client), patch.object(
        mod, "verify_approval", lambda *a, **k: {"ok": True}
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", approved=True, approval_id="x"
        )
    assert result["status"] == "error"
    _no_exception_text(result)


def test_log_export_failure_is_partial_without_exception_text():
    """Parameter written but the export toggle failed → partial, stated plainly."""
    client = _FakeDocDB(raise_on="modify_cluster")
    with _with_client(client), patch.object(
        mod, "verify_approval", lambda *a, **k: {"ok": True}
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", approved=True, approval_id="x"
        )
    assert result["status"] == "partial"
    assert len(client.modify_pg_calls) == 1
    _no_exception_text(result)


def test_client_creation_failure_is_error_without_exception_text():
    def boom(cid, service):
        raise RuntimeError("assume-role boom: internal-detail-42")

    with patch.object(mod, "client_for_cluster", boom):
        result = set_docdb_profiler_impl(MagicMock(), cluster_id="docdb-1")
    assert result["status"] == "error"
    _no_exception_text(result)


def test_no_mongo_path_left_in_module():
    """The Mongo-protocol path is gone: no pymongo factory hook, no write-secret
    resolution, no pymongo import."""
    assert not hasattr(mod, "_CLIENT_FACTORY")
    assert not hasattr(mod, "_write_creds")
    with open(mod.__file__) as f:
        src = f.read()
    assert "import pymongo" not in src
    assert "MongoClient" not in src
