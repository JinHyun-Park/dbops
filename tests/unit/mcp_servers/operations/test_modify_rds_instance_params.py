"""modify_rds_instance_params: the INSTANCE parameter-group write (E-3).

Grounding for the fixture values used below, all read live and READ-ONLY
(`aws rds describe-db-parameters`, ap-northeast-2, 2026-07-29):
  dbops-demo-mysql -> DBParameterGroups [{'DBParameterGroupName':
      'dbops-demo-mysql84', 'ParameterApplyStatus': 'in-sync'}]. That group has
      536 parameters, of which 149 have IsModifiable=false. innodb_buffer_pool_size
      is dynamic/modifiable with NO ParameterValue (engine default),
      max_connections holds the FORMULA string '{DBInstanceClassMemory/12582880}',
      explicit_defaults_for_timestamp is static/modifiable with value '1', and
      log_bin is static with IsModifiable=FALSE. 6 describe_db_parameters pages.
  dbops-demo-mssql -> 'default.sqlserver-ex-15.0', i.e. an AWS-managed default
      group, which is what makes the default.* refusal live-groundable. That
      group has 117 parameters, 44 of them IsModifiable=false (xp_cmdshell,
      'agent xps', 'min server memory (mb)', 'recovery interval (min)',
      'lightweight pooling', 'priority boost' among them), and EVERY name is
      lower case while sys.configurations spells the same options in mixed case.

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

# MEASURED shapes from dbops-demo-mysql84. IsModifiable is carried on EVERY one
# of them because describe_db_parameters carries it on every real parameter: the
# fixtures used to omit it, which is precisely how the tool came to read the field
# and drop it with no test noticing.
P_DYNAMIC = {"ParameterName": "innodb_buffer_pool_size", "ApplyType": "dynamic",
             "IsModifiable": True}
P_FORMULA = {"ParameterName": "max_connections", "ApplyType": "dynamic",
             "IsModifiable": True,
             "ParameterValue": "{DBInstanceClassMemory/12582880}"}
P_STATIC = {"ParameterName": "explicit_defaults_for_timestamp",
            "ParameterValue": "1", "ApplyType": "static", "IsModifiable": True}
# The two shapes finding 1 is about, both MEASURED IsModifiable=false.
P_FIXED_MYSQL = {"ParameterName": "log_bin", "ApplyType": "static",
                 "IsModifiable": False}
P_FIXED_MSSQL = {"ParameterName": "xp_cmdshell", "ApplyType": "dynamic",
                 "IsModifiable": False, "ParameterValue": "0"}
# SQL Server, where the API's spelling and the product's display differ by case.
# API name (describe_db_parameters) vs sys.configurations name (Configuration tab).
P_MSSQL_MEM = {"ParameterName": "max server memory (mb)", "ApplyType": "dynamic",
               "IsModifiable": True,
               "ParameterValue": "{DBInstanceClassMemory/1191564}"}
DISPLAY_MSSQL_MEM = "max server memory (MB)"


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


def test_a_describe_failure_is_not_reported_as_a_missing_instance():
    """"we could not ask" and "RDS says there is no such instance" are different
    answers, and only the second one makes "check the identifier" actionable
    advice. Both used to return not_applicable with that same reason, which sent
    a DBA to fix an identifier that a throttle or an AccessDenied had nothing to
    do with. Same distinction _find_parameter draws for the parameter scan."""
    rds = MagicMock()
    rds.describe_db_instances.side_effect = Exception(
        "ThrottlingException boom: arn:aws:rds:ap-northeast-2:830858425797:db:x")
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "lookup_failed"
    assert "식별자" not in out["reason"]
    # Static reason only: no raw exception text in a response payload, ever.
    blob = " ".join(str(v) for v in out.values())
    for leak in ("boom", "Throttling", "arn:aws:rds", "Exception"):
        assert leak not in blob, f"raw exception text leaked: {out}"
    rds.modify_db_parameter_group.assert_not_called()


def test_unresolvable_instance_refuses():
    """RDS answered with an empty DBInstances list, i.e. no such instance."""
    rds = MagicMock()
    rds.describe_db_instances.return_value = {"DBInstances": []}
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "not_applicable"
    assert "식별자" in out["reason"]
    rds.modify_db_parameter_group.assert_not_called()


# ---------------------------------------------------------------------------
# IsModifiable: the precondition that has to be answered BEFORE the guard runs
#
# describe_db_parameters carries IsModifiable on every parameter and
# modify_db_parameter_group refuses the false ones. MEASURED live: 149 of the 536
# parameters in dbops-demo-mysql84 and 44 of the 117 in default.sqlserver-ex-15.0
# are IsModifiable=false, and 6 of those are options this product DISPLAYS on the
# Configuration tab (xp_cmdshell, 'agent xps', 'min server memory (mb)',
# 'recovery interval (min)', 'lightweight pooling', 'priority boost'). So a DBA
# reading a value off the dashboard and asking to change it is the ordinary path
# into this branch, not an exotic one.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cluster,group,param,name", [
    ("dbops-demo-mysql", "dbops-demo-mysql84", P_FIXED_MYSQL, "log_bin"),
    ("dbops-demo-mssql", "custom-mssql-pg", P_FIXED_MSSQL, "xp_cmdshell"),
])
def test_non_modifiable_parameter_is_refused_before_the_approval_is_consumed(
        cluster, group, param, name):
    """THE BURNT-APPROVAL ORDERING. verify_approval is the only thing that
    consumes the single-use approval, so "not called" is exactly "not consumed".

    Pre-fix behaviour, MEASURED: status modify_failed, verify_approval called
    once (approval gone), modify_db_parameter_group called and rejected by AWS,
    and the reason did not even name the parameter."""
    guard = MagicMock(return_value={"ok": True})
    rds = _rds([group, group], [param])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds), \
         patch.object(M, "verify_approval", guard):
        out = M.modify_rds_instance_params_impl(
            None, cluster, parameter_name=name, value="1",
            parameter_group=group, approved=True, approval_id="appr-1")
    assert out["status"] == "not_modifiable"
    guard.assert_not_called()
    rds.modify_db_parameter_group.assert_not_called()
    # "say which parameter is not modifiable": a DBA must not have to guess.
    assert name in out["reason"]
    assert name == out["parameter"]
    assert "IsModifiable" in out["reason"]


def test_non_modifiable_parameter_never_reaches_approval_required():
    """The preview leg must refuse too. Offering an approval card for a change
    the API will reject is how the approval gets minted in the first place."""
    rds = _rds(["dbops-demo-mysql84"], [P_FIXED_MYSQL])
    out = _call(rds, parameter_name="log_bin", value="ON")
    assert out["status"] == "not_modifiable"
    assert out["apply_type"] == "static"


def test_a_parameter_without_the_field_is_not_declared_non_modifiable():
    """Only an explicit False refuses. A response that does not carry
    IsModifiable has not told us the parameter is fixed, and reporting it as
    fixed would be a negative the data does not support."""
    no_field = {"ParameterName": "innodb_buffer_pool_size", "ApplyType": "dynamic"}
    rds = _rds(["dbops-demo-mysql84"], [no_field])
    out = _call(rds, parameter_name="innodb_buffer_pool_size", value="1")
    assert out["status"] == "approval_required"


def test_modifiable_true_still_previews():
    rds = _rds(["dbops-demo-mysql84"], [P_DYNAMIC])
    assert _call(rds, parameter_name="innodb_buffer_pool_size",
                 value="1")["status"] == "approval_required"


# ---------------------------------------------------------------------------
# The name the product DISPLAYS has to be a name the tool ACCEPTS
#
# MEASURED: describe_db_parameters names every SQL Server parameter in lower
# case ('max server memory (mb)', 'agent xps'), while sys.configurations, which
# is what sp_configure and this product's Configuration tab show, uses mixed case
# ('max server memory (MB)', 'Agent XPs'). 7 of the 23 curated option names in
# mssql_settings._TRACKED differ from the API only by case, MEASURED by executing
# the collector's own statement live and diffing against describe_db_parameters. Neither group has any
# pair of names differing only by case (117 and 536 names checked), so folding
# case cannot merge two distinct parameters.
# ---------------------------------------------------------------------------

def test_the_displayed_sys_configurations_name_is_accepted():
    """Pre-fix, MEASURED: 'max server memory (MB)' -> unknown_parameter while
    'max server memory (mb)' -> approval_required, i.e. the product rejected the
    name it puts on the screen."""
    rds = _rds(["custom-mssql-pg"], [P_MSSQL_MEM])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds):
        out = M.modify_rds_instance_params_impl(
            None, "dbops-demo-mssql", parameter_name=DISPLAY_MSSQL_MEM, value="1024")
    assert out["status"] == "approval_required"
    # ... and it comes back under the API's spelling, because that is what a
    # modify_db_parameter_group call will carry.
    assert out["parameter"] == "max server memory (mb)"
    assert out["current_value"] == "{DBInstanceClassMemory/1191564}"


def test_the_api_spelling_is_what_gets_written_not_the_typed_one():
    rds = _rds(["custom-mssql-pg", "custom-mssql-pg"], [P_MSSQL_MEM])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds), \
         patch.object(M, "verify_approval", lambda *a, **k: {"ok": True}):
        out = M.modify_rds_instance_params_impl(
            None, "dbops-demo-mssql", parameter_name=DISPLAY_MSSQL_MEM,
            value="1024", parameter_group="custom-mssql-pg",
            approved=True, approval_id="u")
    assert out["status"] == "modified"
    assert rds.modify_db_parameter_group.call_args.kwargs["Parameters"][0][
        "ParameterName"] == "max server memory (mb)"
    assert out["parameter"] == "max server memory (mb)"


def test_a_non_modifiable_parameter_is_matched_case_insensitively_too():
    """'Agent XPs' on screen, 'agent xps' in the API, IsModifiable=false. The
    refusal has to fire for the displayed name as well, or the burnt-approval
    path stays open for exactly the names a DBA reads off the dashboard."""
    api_row = {"ParameterName": "agent xps", "ApplyType": "dynamic",
               "IsModifiable": False, "ParameterValue": "1"}
    guard = MagicMock(return_value={"ok": True})
    rds = _rds(["custom-mssql-pg", "custom-mssql-pg"], [api_row])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds), \
         patch.object(M, "verify_approval", guard):
        out = M.modify_rds_instance_params_impl(
            None, "dbops-demo-mssql", parameter_name="Agent XPs", value="0",
            parameter_group="custom-mssql-pg", approved=True, approval_id="u")
    assert out["status"] == "not_modifiable"
    guard.assert_not_called()


def test_a_name_that_is_wrong_beyond_case_is_still_unknown():
    """Case folding must not turn the honest "no such parameter" answer into a
    fuzzy match."""
    rds = _rds(["custom-mssql-pg"], [P_MSSQL_MEM])
    out = _call(rds, parameter_name="max server memory", value="1024")
    assert out["status"] == "unknown_parameter"


def test_the_approval_hash_ignores_parameter_name_case():
    """Both legs of the tool now send the API's spelling, but the agent fills
    action_details itself. If the hash were case-sensitive, an approval
    registered from the DISPLAYED name could never be executed: the DBA would
    approve and be told to approve again, forever."""
    base = {"cluster_id": "i", "parameter_name": "max server memory (mb)",
            "value": "1024", "parameter_group": "pg-a"}
    h = lambda d: G.canonical_action_hash(ACTION, d)  # noqa: E731
    assert h(base) == h({**base, "parameter_name": "max server memory (MB)"})
    assert h(base) == h({**base, "parameter_name": "MAX SERVER MEMORY (MB)"})
    # Still a different parameter, not just a different casing.
    assert h(base) != h({**base, "parameter_name": "min server memory (mb)"})


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
    out = _call(rds, parameter_name="explicit_defaults_for_timestamp", value="1")
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


def test_group_drift_is_refused_BEFORE_the_approval_is_consumed():
    """The approval is hash-bound to dbops-demo-mysql84. If the instance points
    somewhere else, writing there would change an instance the DBA never saw.

    THE ORDERING IS THE POINT. `live_group` is read in this same invocation, so
    the mismatch is known before the guard runs. It used to be found by a SECOND
    describe_db_instances AFTER the consume: MEASURED pre-fix, status
    state_changed with verify_approval called ONCE, i.e. the DBA lost the approval
    AND got no change, on a condition that was already visible. Post-fix:
    state_changed with verify_approval calls 0."""
    guard = MagicMock(return_value={"ok": True})
    rds = _rds(["someone-elses-pg"], [P_DYNAMIC])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds), \
         patch.object(M, "verify_approval", guard):
        out = M.modify_rds_instance_params_impl(
            None, "dbops-demo-mysql", parameter_name="innodb_buffer_pool_size",
            value="1", parameter_group="dbops-demo-mysql84",
            approved=True, approval_id="u")
    assert out["status"] == "state_changed"
    guard.assert_not_called()
    rds.modify_db_parameter_group.assert_not_called()
    # Both group names, so the DBA can see WHICH way it drifted.
    assert out["parameter_group"] == "someone-elses-pg"
    assert out["approved_parameter_group"] == "dbops-demo-mysql84"


def test_drift_to_a_default_group_is_refused_before_the_approval():
    """A drift onto an AWS-default group is caught by the default.* refusal, which
    also sits before the guard."""
    guard = MagicMock(return_value={"ok": True})
    rds = _rds(["default.mysql8.4"], [P_DYNAMIC])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds), \
         patch.object(M, "verify_approval", guard):
        out = M.modify_rds_instance_params_impl(
            None, "dbops-demo-mysql", parameter_name="innodb_buffer_pool_size",
            value="1", parameter_group="dbops-demo-mysql84",
            approved=True, approval_id="u")
    assert out["status"] == "default_group_refused"
    guard.assert_not_called()
    rds.modify_db_parameter_group.assert_not_called()


def test_an_omitted_parameter_group_does_not_consume_the_approval():
    """An agent that leaves parameter_group out used to reach verify_approval and
    fail the payload hash. That was closed but it CONSUMED nothing only by luck of
    where the guard returns; the comparison answers it here instead, and the
    reason must not claim the group "changed" when the arg was simply missing."""
    guard = MagicMock(return_value={"ok": True})
    rds = _rds(["dbops-demo-mysql84"], [P_DYNAMIC])
    with patch.object(M, "client_for_cluster", lambda cid, svc: rds), \
         patch.object(M, "verify_approval", guard):
        out = M.modify_rds_instance_params_impl(
            None, "dbops-demo-mysql", parameter_name="innodb_buffer_pool_size",
            value="1", parameter_group="", approved=True, approval_id="u")
    assert out["status"] == "state_changed"
    guard.assert_not_called()
    assert "다릅니다" in out["reason"]


def test_no_second_describe_runs_after_the_consume():
    """The post-guard re-read is GONE, not merely reordered. It could not observe
    anything the pre-guard read did not (both run at execute time), and any
    refusal it produced arrived with the approval already spent. This pins the
    absence: one describe_db_instances for the whole write."""
    rds = _rds(["pg-a"], [P_DYNAMIC])
    out = _call(rds, guard_ok={"ok": True},
                parameter_name="innodb_buffer_pool_size", value="1",
                parameter_group="pg-a", approved=True, approval_id="u")
    assert out["status"] == "modified"
    assert rds.describe_db_instances.call_count == 1


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
                parameter_name="explicit_defaults_for_timestamp", value="1",
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
