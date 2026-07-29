"""modify_rds_instance_params: the INSTANCE parameter-group write (E-3).

Grounding for the fixture values used below, all read live and READ-ONLY:
  dbops-demo-mysql -> DBParameterGroups [{'DBParameterGroupName':
      'dbops-demo-mysql84', 'ParameterApplyStatus': 'in-sync'}], and in that
      group innodb_buffer_pool_size has ApplyType 'dynamic' with NO
      ParameterValue (engine default), while max_connections holds the FORMULA
      string '{DBInstanceClassMemory/12582880}'. 6 describe_db_parameters pages.
  dbops-demo-mssql -> 'default.sqlserver-ex-15.0', i.e. an AWS-managed default
      group, which is what makes the default.* refusal live-groundable.

The formula string is the reason the approval hash binds `value` as a STRING:
a parameter value is not necessarily a number.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:t")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:t")

import mcp_servers.operations.handler as ops_handler  # noqa: E402
import mcp_servers.operations.tools.modify_rds_instance_params as M  # noqa: E402
from mcp_servers.shared import approval_guard as G  # noqa: E402

ACTION = "modify_rds_instance_params"

# MEASURED shapes from dbops-demo-mysql84.
P_DYNAMIC = {"ParameterName": "innodb_buffer_pool_size", "ApplyType": "dynamic"}
P_FORMULA = {"ParameterName": "max_connections", "ApplyType": "dynamic",
             "ParameterValue": "{DBInstanceClassMemory/12582880}"}
P_STATIC = {"ParameterName": "log_bin_trust_function_creators",
            "ParameterValue": "0", "ApplyType": "static"}


def _rds(groups, params=None, pages=None):
    """groups: one group name per describe_db_instances call, in order."""
    r = MagicMock()
    r.describe_db_instances.side_effect = [
        {"DBInstances": [{"DBParameterGroups": [{"DBParameterGroupName": g}]}]}
        for g in groups
    ]
    if pages is not None:
        r.describe_db_parameters.side_effect = pages
    else:
        r.describe_db_parameters.return_value = {"Parameters": params or []}
    return r


def _call(rds, guard_ok=None, **kw):
    ctx = patch.object(M, "client_for_cluster", lambda cid, svc: rds)
    if guard_ok is None:
        with ctx:
            return M.modify_rds_instance_params_impl(None, "dbops-demo-mysql", **kw)
    with ctx, patch.object(M, "verify_approval",
                           lambda *a, **k: dict(guard_ok)):
        return M.modify_rds_instance_params_impl(None, "dbops-demo-mysql", **kw)


class _Ctx:
    def __init__(self, tool):
        self.client_context = type("cc", (), {"custom": {"tool_name": tool}})()


# ---------------------------------------------------------------------------
# Engine gate: rds_instance only, FAIL-CLOSED
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("family", ["relational", "documentdb", "dynamodb",
                                    "elasticache", None])
def test_every_other_family_is_refused_before_the_impl_runs(family):
    """An Aurora cluster has a CLUSTER parameter group, not an instance one, and
    the non-relational families have neither. The refusal must happen at the gate
    so no AWS call is made with the wrong resource type."""
    spy = MagicMock(return_value={"status": "should not run"})
    with patch.object(ops_handler, "_resolve_family", lambda cid: family), \
         patch.dict(ops_handler.TOOLS[ACTION], {"impl": spy}):
        raw = ops_handler.lambda_handler(
            {"cluster_id": "c", "parameter_name": "p", "value": "v"}, _Ctx(ACTION))
    import json
    out = json.loads(raw["content"][0]["text"])
    assert out["status"] == "unsupported_engine"
    spy.assert_not_called()
    if family is None:
        assert "could not be resolved" in out["reason"]
    else:
        # The reason must be true FOR THIS FAMILY: the tool is instance-only.
        assert "RDS 인스턴스" in out["reason"]


def test_rds_instance_reaches_the_impl():
    spy = MagicMock(return_value={"status": "ran"})
    with patch.object(ops_handler, "_resolve_family", lambda cid: "rds_instance"), \
         patch.dict(ops_handler.TOOLS[ACTION], {"impl": spy}):
        raw = ops_handler.lambda_handler(
            {"cluster_id": "c", "parameter_name": "p", "value": "v"}, _Ctx(ACTION))
    assert '"ran"' in raw["content"][0]["text"]
    spy.assert_called_once()


def test_aurora_cluster_parameter_tool_still_refuses_this_family():
    """RELATIONAL REGRESSION PIN, both directions: adding the instance tool must
    not make modify_parameter reachable for rds_instance, and vice versa."""
    import json
    spy = MagicMock(return_value={"status": "should not run"})
    with patch.object(ops_handler, "_resolve_family", lambda cid: "rds_instance"), \
         patch.dict(ops_handler.TOOLS["modify_parameter"], {"impl": spy}):
        raw = ops_handler.lambda_handler(
            {"cluster_id": "c", "parameter_name": "p", "value": "v"},
            _Ctx("modify_parameter"))
    assert json.loads(raw["content"][0]["text"])["status"] == "unsupported_engine"
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# Static refusals, all BEFORE verify_approval so an approval is never burnt
# ---------------------------------------------------------------------------

def test_default_group_is_refused_and_the_approval_is_not_consumed():
    """MEASURED: dbops-demo-mssql uses default.sqlserver-ex-15.0, which is
    immutable. The refusal must come before verify_approval, or an approval is
    consumed for a change that can never happen (and the retry dies with
    'already consumed')."""
    guard = MagicMock(return_value={"ok": True})
    rds = _rds(["default.sqlserver-ex-15.0"], [P_DYNAMIC])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds), \
         patch.object(M, "verify_approval", guard):
        out = M.modify_rds_instance_params_impl(
            None, "dbops-demo-mssql", parameter_name="max degree of parallelism",
            value="2", parameter_group="default.sqlserver-ex-15.0",
            approved=True, approval_id="u")
    assert out["status"] == "default_group_refused"
    guard.assert_not_called()
    rds.modify_db_parameter_group.assert_not_called()
    assert "커스텀 DB 파라미터 그룹" in out["reason"]


def test_unknown_parameter_is_refused_before_the_approval():
    guard = MagicMock(return_value={"ok": True})
    rds = _rds(["dbops-demo-mysql84"], [P_DYNAMIC])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds), \
         patch.object(M, "verify_approval", guard):
        out = M.modify_rds_instance_params_impl(
            None, "dbops-demo-mysql", parameter_name="not_a_real_parameter",
            value="1", parameter_group="dbops-demo-mysql84",
            approved=True, approval_id="u")
    assert out["status"] == "unknown_parameter"
    guard.assert_not_called()
    rds.modify_db_parameter_group.assert_not_called()


def test_describe_failure_is_lookup_failed_not_unknown_parameter():
    """"the group says this parameter does not exist" and "we could not ask" are
    different answers. Only the first is a safe refusal; conflating them would
    tell a DBA their parameter name is wrong when the call simply failed."""
    rds = _rds(["dbops-demo-mysql84"], pages=Exception("AccessDenied"))
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "lookup_failed"


def test_missing_value_and_name_are_refused():
    rds = _rds(["dbops-demo-mysql84"], [P_DYNAMIC])
    assert _call(rds, parameter_name="", value="1")["status"] == "invalid_request"
    rds = _rds(["dbops-demo-mysql84"], [P_DYNAMIC])
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="  ")
    assert out["status"] == "invalid_request"
    # An empty value is NOT silently treated as "reset to default".
    assert "되돌리는 것은" in out["reason"]


def test_instance_with_no_parameter_group_refuses():
    rds = _rds([""], [P_DYNAMIC])
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "no_parameter_group"


def test_unresolvable_instance_refuses():
    rds = MagicMock()
    rds.describe_db_instances.side_effect = Exception("DBInstanceNotFound")
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "not_applicable"


# ---------------------------------------------------------------------------
# ApplyMethod comes from the parameter's own ApplyType
# ---------------------------------------------------------------------------

def test_dynamic_parameter_previews_immediate():
    """MEASURED: innodb_buffer_pool_size is ApplyType 'dynamic' in
    dbops-demo-mysql84 (MySQL 8.x made it dynamic). Forcing pending-reboot would
    make the DBA restart for a change that needs no downtime."""
    rds = _rds(["dbops-demo-mysql84"], [P_DYNAMIC])
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="536870912")
    assert out["status"] == "approval_required"
    assert out["apply_type"] == "dynamic"
    assert out["apply_method"] == "immediate"
    assert out["parameter_group"] == "dbops-demo-mysql84"
    # No ParameterValue in the group means "engine default", NOT empty string.
    assert out["current_value"] is None
    assert "즉시 반영" in out["cli_preview"]


def test_static_parameter_previews_pending_reboot():
    rds = _rds(["dbops-demo-mysql84"], [P_STATIC])
    out = _call(rds, parameter_name="log_bin_trust_function_creators", value="1")
    assert out["apply_method"] == "pending-reboot"
    assert "재시작 후에" in out["cli_preview"]


def test_unrecognised_apply_type_takes_the_conservative_branch():
    rds = _rds(["g"], [{"ParameterName": "p", "ApplyType": "something-new"}])
    out = _call(rds, parameter_name="p", value="1")
    assert out["apply_method"] == "pending-reboot"


def test_preview_shows_the_formula_value_verbatim():
    """MEASURED: max_connections holds '{DBInstanceClassMemory/12582880}'. The
    preview must show what is actually in the group, not a resolved number."""
    rds = _rds(["dbops-demo-mysql84"], [P_FORMULA])
    out = _call(rds, parameter_name="max_connections", value="200")
    assert out["current_value"] == "{DBInstanceClassMemory/12582880}"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_parameter_found_on_a_later_page():
    """MEASURED 6 pages for dbops-demo-mysql84, so a single-page scan would miss
    most parameters."""
    rds = _rds(["g"], pages=[
        {"Parameters": [{"ParameterName": "other", "ApplyType": "dynamic"}], "Marker": "m1"},
        {"Parameters": [{"ParameterName": "other2", "ApplyType": "dynamic"}], "Marker": "m2"},
        {"Parameters": [P_DYNAMIC]},
    ])
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "approval_required"
    assert rds.describe_db_parameters.call_count == 3


def test_non_string_marker_terminates_the_scan():
    """A bare MagicMock Marker is truthy. Without the isinstance check the scan
    would loop until the page cap on every miss (and hang on an unbounded one)."""
    rds = _rds(["g"], pages=[{"Parameters": [], "Marker": MagicMock()}])
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "unknown_parameter"
    assert rds.describe_db_parameters.call_count == 1


def test_page_cap_reports_lookup_failed_not_unknown_parameter():
    """Running out of pages means the answer is UNKNOWN, so it must not be
    reported as "this parameter does not exist"."""
    rds = _rds(["g"], pages=[{"Parameters": [], "Marker": f"m{i}"}
                             for i in range(M._MAX_PARAM_PAGES + 2)])
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "lookup_failed"
    assert rds.describe_db_parameters.call_count == M._MAX_PARAM_PAGES


# ---------------------------------------------------------------------------
# Approval binding + TOCTOU
# ---------------------------------------------------------------------------

def test_approval_hash_binds_instance_parameter_value_and_group():
    base = {"cluster_id": "i", "parameter_name": "max_connections",
            "value": "200", "parameter_group": "pg-a"}
    h = lambda d: G.canonical_action_hash(ACTION, d)  # noqa: E731
    assert h(base) == h(dict(base))
    assert h(base) != h({**base, "parameter_group": "pg-b"})
    assert h(base) != h({**base, "value": "201"})
    assert h(base) != h({**base, "parameter_name": "max_connect"})
    assert h(base) != h({**base, "cluster_id": "j"})


def test_value_is_bound_as_a_string_not_numerically_normalised():
    """_norm_val collapses any numeric-looking string to float, so "0"/"0.0"/"00"
    and "1e3"/"1000" would share a hash. A parameter group stores the string
    verbatim, so those are different writes."""
    b = {"cluster_id": "i", "parameter_name": "p", "parameter_group": "g"}
    h = lambda v: G.canonical_action_hash(ACTION, {**b, "value": v})  # noqa: E731
    assert h("0") != h("0.0")
    assert h("1000") != h("1e3")
    assert h("ON") != h("on")


def test_guard_denial_writes_nothing():
    rds = _rds(["dbops-demo-mysql84"], [P_DYNAMIC])
    out = _call(rds, guard_ok={"ok": False, "reason": "already consumed"},
                parameter_name="innodb_buffer_pool_size", value="1",
                parameter_group="dbops-demo-mysql84", approved=True, approval_id="u")
    assert out["status"] == "approval_denied"
    assert out["reason"] == "already consumed"
    rds.modify_db_parameter_group.assert_not_called()


def test_group_drift_after_approval_is_refused():
    """The approval pinned dbops-demo-mysql84. If the instance now points at
    another group, writing there would change an instance the DBA never saw."""
    rds = _rds(["dbops-demo-mysql84", "someone-elses-pg"], [P_DYNAMIC])
    out = _call(rds, guard_ok={"ok": True},
                parameter_name="innodb_buffer_pool_size", value="1",
                parameter_group="dbops-demo-mysql84", approved=True, approval_id="u")
    assert out["status"] == "state_changed"
    rds.modify_db_parameter_group.assert_not_called()


def test_drift_to_a_default_group_after_approval_is_refused():
    rds = _rds(["dbops-demo-mysql84", "default.mysql8.4"], [P_DYNAMIC])
    out = _call(rds, guard_ok={"ok": True},
                parameter_name="innodb_buffer_pool_size", value="1",
                parameter_group="dbops-demo-mysql84", approved=True, approval_id="u")
    assert out["status"] in ("state_changed", "default_group_refused")
    rds.modify_db_parameter_group.assert_not_called()


def test_instance_unreadable_after_approval_is_refused():
    rds = MagicMock()
    rds.describe_db_instances.side_effect = [
        {"DBInstances": [{"DBParameterGroups": [{"DBParameterGroupName": "pg-a"}]}]},
        Exception("throttled"),
    ]
    rds.describe_db_parameters.return_value = {"Parameters": [P_DYNAMIC]}
    out = _call(rds, guard_ok={"ok": True},
                parameter_name="innodb_buffer_pool_size", value="1",
                parameter_group="pg-a", approved=True, approval_id="u")
    assert out["status"] == "not_applicable"
    rds.modify_db_parameter_group.assert_not_called()


# ---------------------------------------------------------------------------
# The write itself
# ---------------------------------------------------------------------------

def test_dynamic_write_sends_immediate_and_reports_applied():
    rds = _rds(["pg-a", "pg-a"], [P_DYNAMIC])
    out = _call(rds, guard_ok={"ok": True},
                parameter_name="innodb_buffer_pool_size", value="536870912",
                parameter_group="pg-a", approved=True, approval_id="u")
    assert out["status"] == "modified"
    assert out["applied"] is True
    kwargs = rds.modify_db_parameter_group.call_args.kwargs
    assert kwargs["DBParameterGroupName"] == "pg-a"
    assert kwargs["Parameters"] == [{
        "ParameterName": "innodb_buffer_pool_size",
        "ParameterValue": "536870912",
        "ApplyMethod": "immediate",
    }]
    # The INSTANCE API, never the Aurora CLUSTER one.
    rds.modify_db_cluster_parameter_group.assert_not_called()


def test_static_write_reports_not_yet_applied():
    """"approved and modified" must not read as "the server is doing it now" for
    a static parameter, which is the pending-reboot confusion this field exists
    to prevent."""
    rds = _rds(["pg-a", "pg-a"], [P_STATIC])
    out = _call(rds, guard_ok={"ok": True},
                parameter_name="log_bin_trust_function_creators", value="1",
                parameter_group="pg-a", approved=True, approval_id="u")
    assert out["applied"] is False
    assert out["apply_method"] == "pending-reboot"
    assert "인스턴스 재시작이 필요" in out["note"]
    assert rds.modify_db_parameter_group.call_args.kwargs[
        "Parameters"][0]["ApplyMethod"] == "pending-reboot"


def test_note_warns_that_a_shared_group_hits_other_instances():
    rds = _rds(["pg-a", "pg-a"], [P_DYNAMIC])
    out = _call(rds, guard_ok={"ok": True},
                parameter_name="innodb_buffer_pool_size", value="1",
                parameter_group="pg-a", approved=True, approval_id="u")
    assert "다른 인스턴스" in out["note"]


def test_write_failure_returns_no_exception_text():
    rds = _rds(["pg-a", "pg-a"], [P_DYNAMIC])
    rds.modify_db_parameter_group.side_effect = Exception(
        "InvalidParameterValue: secret arn:aws:secretsmanager:x")
    out = _call(rds, guard_ok={"ok": True},
                parameter_name="innodb_buffer_pool_size", value="1",
                parameter_group="pg-a", approved=True, approval_id="u")
    assert out["status"] == "modify_failed"
    blob = repr(out)
    assert "secretsmanager" not in blob
    assert "InvalidParameterValue" not in blob


# ---------------------------------------------------------------------------
# Approval registration
# ---------------------------------------------------------------------------

def test_request_approval_accepts_the_new_action_type():
    """A write tool whose action_type is missing from the request_approval enum
    dead-ends the approval loop (the P0 this parity test family exists for)."""
    import mcp_servers.operations.tools.request_approval as RA
    with patch.dict(os.environ, {"APPROVALS_TABLE": "t"}), \
         patch.object(RA, "boto3") as b:
        b.resource.return_value.Table.return_value.put_item.return_value = {}
        out = RA.request_approval_impl(
            None, cluster_id="dbops-demo-mysql", action_type=ACTION,
            action_details={"cluster_id": "dbops-demo-mysql",
                            "parameter_name": "innodb_buffer_pool_size",
                            "value": "536870912",
                            "parameter_group": "dbops-demo-mysql84"})
    assert out["status"] != "error", out
    assert out.get("approval_id")
