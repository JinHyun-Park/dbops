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
    # AWS delivers profiler logs to a PER-CLUSTER group, not a shared one.
    assert result["log_group"] == "/aws/docdb/docdb-1/profiler"
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
    {enabled, threshold_ms, sampling_rate} payload AND the RESOLVED parameter
    group. The group is the actual write target and is shared across clusters,
    so it must be inside the hash."""
    client = _FakeDocDB()
    guard = MagicMock(return_value={"ok": True})
    with _with_client(client), patch.object(mod, "verify_approval", guard):
        set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, threshold_ms=200,
            sampling_rate=0.25, approved=True, approval_id="appr-1",
        )
    guard.assert_called_once_with(
        "appr-1", "docdb-1", "set_docdb_profiler",
        payload={
            "enabled": True,
            "threshold_ms": 200,
            "sampling_rate": 0.25,
            "parameter_group": _CUSTOM_PG,
        },
    )


def test_parameter_group_reassignment_between_approval_and_execute_is_refused():
    """TOCTOU: a parameter group is SHARED by every cluster attached to it. If
    the cluster is re-pointed at another group after the DBA approved, the
    approval hash no longer matches and nothing is written.

    Simulated the way the guard really behaves: canonical_action_hash over the
    approved details vs over the details the tool submits at execute time."""
    from mcp_servers.shared.approval_guard import canonical_action_hash

    approved_details = {
        "enabled": True,
        "threshold_ms": 200,
        "sampling_rate": 0.25,
        "parameter_group": "pg-approved",
    }
    approved_hash = canonical_action_hash("set_docdb_profiler", approved_details)

    # the live cluster now points at a DIFFERENT (shared) group
    client = _FakeDocDB(pg="pg-someone-elses")

    def fake_guard(approval_id, cluster_id, action_type, payload=None):
        if canonical_action_hash(action_type, payload or {}) != approved_hash:
            return {"ok": False, "reason": "payload_hash mismatch"}
        return {"ok": True}

    with _with_client(client), patch.object(mod, "verify_approval", fake_guard):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, threshold_ms=200,
            sampling_rate=0.25, approved=True, approval_id="appr-1",
        )
    assert result["status"] == "approval_denied"
    assert result["parameter_group"] == "pg-someone-elses"
    # nothing was written to the unapproved group
    assert client.modify_pg_calls == []
    assert client.modify_cluster_calls == []


def test_unchanged_parameter_group_still_passes_the_same_hash():
    """Control for the test above: when the group is unchanged the hash matches
    and the write proceeds, so the new binding does not break the happy path."""
    from mcp_servers.shared.approval_guard import canonical_action_hash

    approved_details = {
        "enabled": True,
        "threshold_ms": 200,
        "sampling_rate": 0.25,
        "parameter_group": _CUSTOM_PG,
    }
    approved_hash = canonical_action_hash("set_docdb_profiler", approved_details)
    client = _FakeDocDB(log_exports=[])

    def fake_guard(approval_id, cluster_id, action_type, payload=None):
        if canonical_action_hash(action_type, payload or {}) != approved_hash:
            return {"ok": False, "reason": "payload_hash mismatch"}
        return {"ok": True}

    with _with_client(client), patch.object(mod, "verify_approval", fake_guard):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, threshold_ms=200,
            sampling_rate=0.25, approved=True, approval_id="appr-1",
        )
    assert result["status"] == "modified"
    assert client.modify_pg_calls[0]["DBClusterParameterGroupName"] == _CUSTOM_PG


def test_log_group_is_per_cluster():
    """Regression: the constant used to be a single shared /aws/docdb/profiler,
    which does not exist. AWS delivers to /aws/docdb/{cluster_id}/profiler."""
    assert mod.profiler_log_group("my-cluster") == "/aws/docdb/my-cluster/profiler"
    assert mod.profiler_log_group("other") == "/aws/docdb/other/profiler"


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


def test_string_flag_refused_before_any_aws_call():
    """An ambiguous `enabled` must never reach a hash or a write. A string flag
    is REFUSED (it used to be coerced locally, while the approval projection used
    bare bool(), so hash("false") == hash(True) and a DBA-approved DISABLE could
    not be executed)."""
    factory = MagicMock(side_effect=AssertionError("must not call AWS"))
    guard = MagicMock(return_value={"ok": True})
    with patch.object(mod, "client_for_cluster", factory), patch.object(
        mod, "verify_approval", guard
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled="false", approved=True,
            approval_id="x",
        )
    assert result["status"] == "error"
    assert "boolean" in result["reason"]
    factory.assert_not_called()
    guard.assert_not_called()


def test_string_true_flag_also_refused():
    """Both directions: "true" is just as ambiguous as "false"."""
    factory = MagicMock(side_effect=AssertionError("must not call AWS"))
    with patch.object(mod, "client_for_cluster", factory):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled="true", approved=True,
            approval_id="x",
        )
    assert result["status"] == "error"
    factory.assert_not_called()


def test_threshold_above_int_max_rejected_no_aws():
    """The advertised range is 50..INT_MAX; the upper end is enforced too, so an
    out-of-range value is refused with a stated reason instead of surfacing as an
    opaque AWS API failure."""
    factory = MagicMock(side_effect=AssertionError("must not call AWS"))
    with patch.object(mod, "client_for_cluster", factory):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True,
            threshold_ms=mod.MAX_THRESHOLD_MS + 1,
        )
    assert result["status"] == "error"
    assert "threshold_ms" in result["reason"]
    factory.assert_not_called()


def test_note_tells_the_operator_how_to_actually_read_the_profiler_log():
    """Enabling the profiler starts billable CloudWatch Logs ingestion, so the
    note must say how to READ it, and say it accurately.

    History: the note first claimed DBOps could not read the group at all. That
    was wrong in both directions. The incident Lambda held logs:StartQuery on
    resources=["*"] (its comment claimed /aws/rds/cluster/* but the scope was
    never applied), so it could read ANY group; E-0 narrowed the IAM to the three
    DB log-group prefixes, which INCLUDES /aws/docdb/*, and search_logs enforces
    the same allowlist. So the profiler log IS readable, but only when log_group
    is passed explicitly, because the default is the cluster error log."""
    client = _FakeDocDB(log_exports=[])
    with _with_client(client), patch.object(
        mod, "verify_approval", lambda *a, **k: {"ok": True}
    ):
        result = set_docdb_profiler_impl(
            MagicMock(), cluster_id="docdb-1", enabled=True, approved=True,
            approval_id="x",
        )
    note = result["note"]
    assert "search_logs" in note
    assert "/aws/docdb/docdb-1/profiler" in note
    # must NOT resurrect the false claim
    assert "조회하지 못합니다" not in note
    # the group is inside the tool's own allowlist, so the advice is actionable
    from mcp_servers.incident.tools.search_logs import ALLOWED_LOG_GROUP_PREFIXES

    assert result["log_group"].startswith(ALLOWED_LOG_GROUP_PREFIXES)


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


def _profiler_tool_description() -> str:
    """The set_docdb_profiler description block from the handler registry, read as
    TEXT so the handler's import-time CacheClient() is not needed."""
    from pathlib import Path

    src = (Path(mod.__file__).resolve().parents[2] / "operations" / "handler.py").read_text()
    start = src.index('"set_docdb_profiler": {')
    return src[start : src.index('"input_schema"', start)]


def test_agent_facing_docs_do_not_claim_the_profiler_log_is_unreadable():
    """FINDING 4: search_logs gained the /aws/docdb/ prefix, but the handler
    description (served to the agent via tools/list) and this module's docstring
    still told the operator to go to the AWS console. The runtime note was already
    corrected, so the three were contradicting each other."""
    desc = _profiler_tool_description()
    assert "search_logs" in desc
    assert "CANNOT query" not in desc
    assert "console" not in desc.lower()

    with open(mod.__file__) as f:
        docstring = f.read().split('"""')[1]
    assert "search_logs" in docstring
    assert "NO profiler-log surface" not in docstring


def test_no_mongo_path_left_in_module():
    """The Mongo-protocol path is gone: no pymongo factory hook, no write-secret
    resolution, no pymongo import."""
    assert not hasattr(mod, "_CLIENT_FACTORY")
    assert not hasattr(mod, "_write_creds")
    with open(mod.__file__) as f:
        src = f.read()
    assert "import pymongo" not in src
    assert "MongoClient" not in src
