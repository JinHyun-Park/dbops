"""Tests for request_approval — verify all action_types including EC-4 are accepted."""
import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BASE = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/operations/tools"


def _load(name):
    p = _BASE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


request_approval = _load("request_approval")


def test_request_approval_accepts_execute_sql():
    """request_approval accepts execute_sql action_type."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cluster", action_type="execute_sql",
                action_details={"sql": "SELECT 1"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "execute_sql"


def test_request_approval_accepts_modify_elasticache_node_type():
    """EC-4: request_approval accepts modify_elasticache_node_type."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="modify_elasticache_node_type",
                action_details={"node_type": "cache.r7g.large"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "modify_elasticache_node_type"


def test_request_approval_accepts_create_elasticache_snapshot():
    """EC-4: request_approval accepts create_elasticache_snapshot."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="create_elasticache_snapshot",
                action_details={"snapshot_name": "snap1"}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "create_elasticache_snapshot"


def test_request_approval_accepts_reboot_elasticache():
    """EC-4: request_approval accepts reboot_elasticache."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="reboot_elasticache",
                action_details={}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "reboot_elasticache"


def test_request_approval_accepts_test_elasticache_failover():
    """EC-4: request_approval accepts test_elasticache_failover."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cache", action_type="test_elasticache_failover",
                action_details={}
            )
            assert result["status"] == "pending"
            assert result["action_type"] == "test_elasticache_failover"


def test_request_approval_rejects_unknown_action_type():
    """request_approval rejects unknown action_types as expected."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            mock_boto3.resource.return_value.Table.return_value = MagicMock()
            result = request_approval.request_approval_impl(
                None, cluster_id="test-cluster", action_type="unknown_action_xyz",
                action_details={}
            )
            assert result["status"] == "error"
            assert "unknown action_type" in result["message"]


# ===== N-①: origin is stamped by the API, NEVER by this tool =================


def _put_item(action_type="create_custom_endpoint", details=None, **kw):
    """Call request_approval and return the DDB Item that was put."""
    details = details or {"endpoint_identifier": "ep1", "endpoint_type": "READER",
                          "static_members": [], "excluded_members": []}
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="c1", action_type=action_type,
                action_details=details, **kw,
            )
    return table.put_item.call_args.kwargs["Item"], result


def test_tool_never_writes_origin():
    """Trust boundary: request_approval must NOT write an origin marker — the
    auto-execute gate keys on origin=="ui", and only the trusted approvals API
    Lambda stamps it (after this tool returns). If the tool wrote origin, the
    agent could mint a UI-looking row via the gateway and get a chat-initiated
    destructive write auto-executed."""
    item, _ = _put_item()
    assert "origin" not in item


def test_tool_rejects_origin_kwarg():
    """origin is not a parameter of this tool — passing it must raise, proving
    the agent has no channel to set it through the tool."""
    import pytest
    with pytest.raises(TypeError):
        _put_item(origin="ui")


def test_created_at_returned_for_api_stamp():
    """The API caller needs (approval_id, created_at) to address the row it just
    created and stamp origin, so both must be in the return."""
    item, result = _put_item()
    assert result["status"] == "pending"
    assert result["approval_id"] == item["approval_id"]
    assert result["created_at"] == item["created_at"]


# ===== E-0: no exception text in the response, profiler range on both paths ===


def test_put_item_failure_has_no_exception_text():
    """The DDB error message can carry the table ARN / account id / the whole
    item, and this string is shown in chat. Static reason, details to the log."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            table.put_item.side_effect = RuntimeError(
                "ValidationException: arn:aws:dynamodb:ap-northeast-2:123456789012:table/secret"
            )
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="c1", action_type="execute_sql",
                action_details={"sql": "SELECT 1"},
            )
    assert result["status"] == "error"
    blob = " ".join(str(v) for v in result.values())
    for leak in ("arn:aws", "123456789012", "ValidationException", "RuntimeError"):
        assert leak not in blob, f"raw exception text leaked: {result}"


def test_profiler_registration_rejects_out_of_range_threshold():
    """FINDING 3: this path mints the payload_hash the write is bound to, so a
    value set_docdb_profiler would refuse must never reach the Approval Center
    (the DBA would approve a change that then dead-ends at execute time)."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="docdb-1", action_type="set_docdb_profiler",
                action_details={"enabled": True, "threshold_ms": 10, "sampling_rate": 1.0},
            )
    assert result["status"] == "error"
    assert "threshold_ms" in result["message"]
    table.put_item.assert_not_called()


def test_profiler_registration_rejects_out_of_range_sampling_rate():
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="docdb-1", action_type="set_docdb_profiler",
                action_details={"enabled": True, "sampling_rate": 1.5},
            )
    assert result["status"] == "error"
    assert "sampling_rate" in result["message"]
    table.put_item.assert_not_called()


def test_profiler_registration_accepts_in_range_and_defaults():
    """Control: an in-range payload (and an omitted knob, which takes the tool
    default) still registers."""
    item, result = _put_item(
        "set_docdb_profiler", {"enabled": False, "threshold_ms": 500}
    )
    assert result["status"] == "pending"
    assert item["action_type"] == "set_docdb_profiler"


# ===== FINDING 1: the registered hash must equal the EXECUTE-side hash ========


class _FakeDocDB:
    """Only the describe the profiler tool needs before verify_approval."""

    def describe_db_clusters(self, **kw):
        return {"DBClusters": [{
            "DBClusterParameterGroup": "pg-a",
            "EnabledCloudwatchLogsExports": [],
        }]}


def _execute_side_payload(**tool_kwargs):
    """The payload set_docdb_profiler actually binds its approval to, captured
    from the real impl (defaults applied) without letting it write anything."""
    import mcp_servers.operations.tools.set_docdb_profiler as prof

    captured = {}

    def fake_guard(approval_id, cluster_id, action_type, payload=None):
        captured["payload"] = payload
        return {"ok": False, "reason": "captured, stop before any write"}

    with patch.object(prof, "client_for_cluster", lambda cid, service: _FakeDocDB()), \
            patch.object(prof, "verify_approval", fake_guard):
        prof.set_docdb_profiler_impl(
            MagicMock(), approved=True, approval_id="appr-1", **tool_kwargs
        )
    return captured["payload"]


def test_profiler_default_registration_hash_equals_execute_side_hash():
    """FINDING 1: set_docdb_profiler applies its defaults (threshold_ms=100,
    sampling_rate=1.0) BEFORE hashing, so a registration that omits those knobs
    must materialize the SAME effective values. Before the fix the omitted knob
    projected as None, the two hashes differed, and every default-path approval
    was permanently deniable: the DBA approves, the agent re-runs, the guard
    burns the row and returns approval_denied."""
    from mcp_servers.shared.approval_guard import canonical_action_hash

    item, _ = _put_item(
        "set_docdb_profiler",
        # exactly what the agent registers when it copies the approval_required
        # response and the operator never mentioned the two knobs
        {"cluster_id": "docdb-1", "enabled": True, "parameter_group": "pg-a"},
    )
    payload = _execute_side_payload(cluster_id="docdb-1", enabled=True)

    assert item["payload_hash"] == canonical_action_hash("set_docdb_profiler", payload)


def test_profiler_registration_card_shows_effective_numbers():
    """Same fix, DBA-visible half: the stored action_details must carry the
    effective values, otherwise the approval card hides what will be written."""
    item, _ = _put_item(
        "set_docdb_profiler", {"cluster_id": "docdb-1", "parameter_group": "pg-a"}
    )
    details = item["action_details"]
    assert details["enabled"] is True
    assert int(details["threshold_ms"]) == 100
    assert float(details["sampling_rate"]) == 1.0


# ===== FINDING 2: an ambiguous (non-bool) flag never enters a hash ============


def test_registration_refuses_string_flag_for_ttl():
    """bare bool("false") is True, so a card reading enabled:"false" hashed
    byte-identically to an executed enabled=True: the DBA approves a DISABLE and
    the agent consumes it for an ENABLE. Refuse at the registration boundary."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="ddb-1", action_type="modify_dynamodb_ttl",
                action_details={"attribute": "expires_at", "enabled": "false"},
            )
    assert result["status"] == "error"
    assert "enabled" in result["message"]
    table.put_item.assert_not_called()


def test_registration_refuses_string_flag_for_pitr_force():
    """force is hashed too (it gates the PITR disable), so a string force is
    equally ambiguous."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="ddb-1", action_type="enable_dynamodb_pitr",
                action_details={"enabled": False, "force": "false"},
            )
    assert result["status"] == "error"
    assert "force" in result["message"]
    table.put_item.assert_not_called()


def test_registration_refuses_string_flag_for_profiler():
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id="docdb-1", action_type="set_docdb_profiler",
                action_details={"enabled": "true", "parameter_group": "pg-a"},
            )
    assert result["status"] == "error"
    assert "enabled" in result["message"]
    table.put_item.assert_not_called()


def test_registration_accepts_real_booleans():
    """Control: real JSON booleans (including False, which must not be read as
    'missing') still register."""
    item, result = _put_item(
        "modify_dynamodb_ttl", {"attribute": "expires_at", "enabled": False}
    )
    assert result["status"] == "pending"
    assert item["action_details"]["enabled"] is False


# ===== a parameter card that can never execute is never minted ================
#
# Both parameter tools refuse an empty parameter_name and an empty value BEFORE
# verify_approval (MEASURED: invalid_request, 0 consumes). This file is the only
# payload_hash minter, and it used to accept both and store them verbatim, so the
# DBA was handed a card that could only ever answer invalid_request. The refusals
# now run on the registration boundary too.
#
# `parameter` is the alias to care about: it is the key BOTH tools'
# approval_required response actually uses, and _project already tolerates it.

_AURORA = {"cluster_id": "pgtsd-demo-aurora-pg", "parameter": "work_mem",
           "value": "8MB", "parameter_group": "pgtsd-demo-cpg"}
_INSTANCE = {"cluster_id": "dbops-demo-mysql",
             "parameter_name": "innodb_buffer_pool_size", "value": "536870912",
             "parameter_group": "dbops-demo-mysql84"}
_PARAM_ACTIONS = [
    ("modify_parameter", _AURORA, "parameter"),
    ("modify_rds_instance_params", _INSTANCE, "parameter_name"),
]


def _register(action_type, details):
    """Returns (result, table) so "no card minted" can be asserted on put_item."""
    with patch.dict(os.environ, {"APPROVALS_TABLE": "approvals"}):
        with patch.object(request_approval, "boto3") as mock_boto3:
            table = MagicMock()
            mock_boto3.resource.return_value.Table.return_value = table
            result = request_approval.request_approval_impl(
                None, cluster_id=details.get("cluster_id", "c1"),
                action_type=action_type, action_details=details)
    return result, table


@pytest.mark.parametrize("action_type,base,namekey", _PARAM_ACTIONS)
@pytest.mark.parametrize("empty", ["", "   ", None])
def test_registration_refuses_an_empty_parameter_name(action_type, base, namekey,
                                                      empty):
    result, table = _register(action_type, {**base, namekey: empty})
    assert result["status"] == "error"
    assert "parameter_name" in result["message"]
    table.put_item.assert_not_called()


@pytest.mark.parametrize("action_type,base,namekey", _PARAM_ACTIONS)
@pytest.mark.parametrize("empty", ["", "   ", None])
def test_registration_refuses_an_empty_value(action_type, base, namekey, empty):
    """Clearing a parameter back to the engine default is a different operation
    (reset_db_*_parameter_group), which is why the tools refuse rather than send
    ParameterValue "". A card for it is unexecutable by construction."""
    result, table = _register(action_type, {**base, "value": empty})
    assert result["status"] == "error"
    assert "value" in result["message"]
    table.put_item.assert_not_called()


@pytest.mark.parametrize("action_type,base,namekey", _PARAM_ACTIONS)
@pytest.mark.parametrize("real", ["0", "off", "OFF", 0])
def test_registration_still_mints_a_falsy_but_real_value(action_type, base,
                                                         namekey, real):
    """Control: "0" and "off" are legitimate parameter values, and both tools
    accept them. Only an actually-empty value is refused, which is why the check
    runs on the STRINGIFIED value: a JSON 0 is falsy and is not empty."""
    result, table = _register(action_type, {**base, "value": real})
    assert result["status"] == "pending"
    table.put_item.assert_called_once()


@pytest.mark.parametrize("action_type,base,namekey", _PARAM_ACTIONS)
def test_registration_hash_is_unmoved_by_the_normalisation(action_type, base,
                                                           namekey):
    """The registration path MINTS the hash the execute leg is verified against,
    so normalising here must not move it. It does not: _project already strips
    parameter_name and value, so the padded card and the clean one project to one
    hash, and that hash is the one the tool computes from the API's spelling."""
    from mcp_servers.shared.approval_guard import canonical_action_hash

    padded, table = _register(
        action_type, {**base, namekey: f"  {base[namekey].upper()}  ",
                      "value": f"  {base['value']}  "})
    assert padded["status"] == "pending"
    item = table.put_item.call_args.kwargs["Item"]
    assert item["payload_hash"] == canonical_action_hash(action_type, base)
    # ...and the card shows what the executor will send, not the padding.
    assert item["action_details"][namekey] == base[namekey].upper()
    assert item["action_details"]["value"] == base["value"]
