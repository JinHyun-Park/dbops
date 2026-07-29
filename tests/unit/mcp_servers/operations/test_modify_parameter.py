"""modify_parameter: the Aurora CLUSTER parameter-group write.

Fixture grounding, all read live and READ-ONLY (boto3
`describe_db_cluster_parameters`, ap-northeast-2, 2026-07-29). No group was
created, modified or deleted:
  pgtsd-demo-cpg (the CUSTOM cluster group on the registered Aurora PG cluster
      pgtsd-demo-aurora-pg): 448 parameters over 5 pages, 39 of them
      IsModifiable=false, 0 missing the IsModifiable field, 0 names that are not
      already lower case, and 279 with NO ParameterValue at all (engine default).
      `config_file` is IsModifiable=false / ApplyType static / ParameterValue
      '/rdsdbdata/config/postgresql.conf'; max_connections is IsModifiable=true
      and holds the formula 'LEAST({DBInstanceClassMemory/9531392},5000)'.
  default.aurora-postgresql15: 416 parameters, 34 IsModifiable=false.
  default.aurora-mysql8.0: 424 parameters, 65 IsModifiable=false.
  5 of the 6 Aurora clusters in the account sit on a default.* cluster group.
"""

from unittest.mock import MagicMock, patch

from mcp_servers.operations.tools.modify_parameter import modify_parameter_impl

CUSTOM_PG = "prod-pg-1-custom-pg15"

# MEASURED shapes. IsModifiable is on EVERY real parameter of every group probed,
# which is why omitting it from a fixture is what let the tool ship with no
# modifiability check at all for as long as it did.
P_OK = {"ParameterName": "max_connections", "ApplyType": "static",
        "IsModifiable": True,
        "ParameterValue": "LEAST({DBInstanceClassMemory/9531392},5000)"}
P_WORK_MEM = {"ParameterName": "work_mem", "ApplyType": "dynamic",
              "IsModifiable": True}
P_FIXED = {"ParameterName": "config_file", "ApplyType": "static",
           "IsModifiable": False,
           "ParameterValue": "/rdsdbdata/config/postgresql.conf"}


def _rds(pg=CUSTOM_PG, params=None, pages=None):
    """An rds double whose cluster sits on `pg` and whose cluster parameter group
    holds `params` (or serves `pages` from describe_db_cluster_parameters)."""
    r = MagicMock()
    r.describe_db_clusters.return_value = {
        "DBClusters": [{"DBClusterParameterGroup": pg}]}
    if pages is not None:
        r.describe_db_cluster_parameters.side_effect = pages
    else:
        r.describe_db_cluster_parameters.return_value = {
            "Parameters": params if params is not None else [P_OK, P_WORK_MEM, P_FIXED]}
    return r


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_modify_parameter_requires_approval(mock_rds_for):
    """No approved=True → approval_required, and nothing is written. The
    parameter group is resolved first (so an impossible change is refused before
    the DBA is asked) and reported on the card."""
    mock_rds = _rds()
    mock_rds_for.return_value = mock_rds
    mock_cache = MagicMock()
    result = modify_parameter_impl(
        mock_cache, cluster_id="prod-pg-1", parameter_name="max_connections", value="200"
    )
    assert result["status"] == "approval_required"
    assert result["parameter"] == "max_connections"
    assert result["value"] == "200"
    assert result["parameter_group"] == CUSTOM_PG
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_modify_parameter_with_approval(mock_rds_for):
    """Approved + cluster on a CUSTOM parameter group → impl applies the
    change via modify_db_cluster_parameter_group. Guard bypassed via env."""
    mock_rds = _rds()
    mock_rds_for.return_value = mock_rds
    mock_cache = MagicMock()
    result = modify_parameter_impl(
        mock_cache, cluster_id="prod-pg-1", parameter_name="max_connections", value="200",
        parameter_group=CUSTOM_PG, approved=True,
    )
    assert result["status"] == "modified"
    assert result["parameter"] == "max_connections"
    assert result["parameter_group"] == CUSTOM_PG
    mock_rds.modify_db_cluster_parameter_group.assert_called_once()
    call_kwargs = mock_rds.modify_db_cluster_parameter_group.call_args.kwargs
    assert call_kwargs["DBClusterParameterGroupName"] == CUSTOM_PG
    assert call_kwargs["Parameters"][0]["ParameterName"] == "max_connections"
    assert call_kwargs["Parameters"][0]["ParameterValue"] == "200"
    # pending-reboot 안내가 결과에 포함돼야 한다 — 에이전트가 "재시작 후
    # 적용"을 사용자에게 전달하게 하는 핵심(승인 후 즉시 반영 오해 방지).
    assert call_kwargs["Parameters"][0]["ApplyMethod"] == "pending-reboot"
    assert result["apply_method"] == "pending-reboot"
    assert result["applied"] is False
    assert "재시작" in result["note"]


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_modify_parameter_refuses_default_group(mock_rds_for):
    """Approved + cluster on the AWS-default parameter group → impl refuses
    and returns default_group_refused without calling modify."""
    mock_rds = _rds("default.aurora-postgresql15")
    mock_rds_for.return_value = mock_rds
    mock_cache = MagicMock()
    result = modify_parameter_impl(
        mock_cache, cluster_id="prod-pg-1", parameter_name="max_connections", value="200", approved=True,
    )
    assert result["status"] == "default_group_refused"
    assert result["parameter_group"].startswith("default.")
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()
    # A group we refuse outright is not worth 5 pages of describe.
    mock_rds.describe_db_cluster_parameters.assert_not_called()
    # The refusal has to say what unblocks it, not just that it is blocked: the
    # DBA's next step is a CUSTOM group. Mirrors modify_rds_instance_params.
    assert "커스텀 클러스터 파라미터 그룹" in result["reason"]


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_lookup_failure_returns_static_reason_no_exception_text(mock_rds_for):
    """describe_db_clusters blowing up must NOT put the exception into the
    response (project hard rule): static Korean reason + module logger only."""
    mock_rds = MagicMock()
    mock_rds_for.return_value = mock_rds
    mock_rds.describe_db_clusters.side_effect = RuntimeError(
        "AccessDenied boom: arn:aws:iam::123456789012:role/secret-ish"
    )
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="64MB",
        approved=True,
    )
    assert result["status"] == "lookup_failed"
    assert "error" not in result
    blob = " ".join(str(v) for v in result.values())
    for leak in ("boom", "AccessDenied", "arn:aws:iam", "RuntimeError"):
        assert leak not in blob, f"raw exception text leaked: {result}"
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()


@patch.dict("os.environ", {"APPROVAL_GUARD_BYPASS": "1"})
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_modify_failure_returns_static_reason_no_exception_text(mock_rds_for):
    mock_rds = _rds()
    mock_rds_for.return_value = mock_rds
    mock_rds.modify_db_cluster_parameter_group.side_effect = RuntimeError(
        "InvalidParameterValue boom: internal-detail-42"
    )
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="64MB",
        parameter_group=CUSTOM_PG, approved=True,
    )
    assert result["status"] == "modify_failed"
    assert "error" not in result
    blob = " ".join(str(v) for v in result.values())
    for leak in ("boom", "InvalidParameterValue", "internal-detail-42"):
        assert leak not in blob, f"raw exception text leaked: {result}"
    # Everything knowable is refused earlier, so what can still fail here is the
    # value's allowed range and permissions, including the cross-account
    # ManagedBy=dbops tag that has to sit on the parameter GROUP. The approval is
    # already spent by this point, so this reason is the DBA's only pointer.
    for pointer in ("값 유효 범위", "권한", "태그"):
        assert pointer in result["reason"], result["reason"]


@patch("mcp_servers.operations.tools.modify_parameter.verify_approval")
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_static_refusals_do_not_burn_the_approval(mock_rds_for, mock_verify):
    """FINDING 3: the approval is SINGLE-USE. Refusing on a static precondition
    (default.* parameter group, describe failure) AFTER consuming it burnt the
    approval and the retry died with "already consumed". Every refusal now runs
    BEFORE verify_approval, mirroring set_docdb_profiler."""
    mock_rds = _rds("default.aurora-postgresql15")
    mock_rds_for.return_value = mock_rds
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="max_connections",
        value="200", approved=True, approval_id="appr-1",
    )
    assert result["status"] == "default_group_refused"
    mock_verify.assert_not_called()
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()

    # same for a lookup failure: the approval must survive for the retry
    mock_verify.reset_mock()
    mock_rds.describe_db_clusters.side_effect = RuntimeError("boom")
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="max_connections",
        value="200", approved=True, approval_id="appr-1",
    )
    assert result["status"] == "lookup_failed"
    mock_verify.assert_not_called()


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_client_init_failure_is_static_error(mock_rds_for):
    """The client factory now runs on the unapproved path too, so its failure has
    to be a static reason instead of an exception escaping the tool."""
    mock_rds_for.side_effect = RuntimeError("assume-role boom: arn:aws:iam::123456789012:role/x")
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="64MB",
    )
    assert result["status"] == "error"
    blob = " ".join(str(v) for v in result.values())
    for leak in ("boom", "arn:aws:iam", "123456789012", "RuntimeError"):
        assert leak not in blob, f"raw exception text leaked: {result}"


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_modify_parameter_approved_without_id_rejected(mock_rds_for):
    """Bare `approved=True` (no approval_id) is rejected by the guard."""
    mock_rds = _rds()
    mock_rds_for.return_value = mock_rds
    with patch.dict("os.environ", {"APPROVALS_TABLE": "approvals"}, clear=True):
        mock_cache = MagicMock()
        result = modify_parameter_impl(
            mock_cache,
            cluster_id="prod-pg-1",
            parameter_name="max_connections",
            value="200",
            parameter_group=CUSTOM_PG,
            approved=True,
        )
        assert result["status"] == "approval_denied"
        assert "approval_id missing" in result["reason"]


# ---------------------------------------------------------------------------
# The parameter has to EXIST and be MODIFIABLE, and both answers have to arrive
# BEFORE verify_approval consumes the DBA's single-use approval.
#
# This tool called describe_db_clusters and then modify_db_cluster_parameter_group
# with no describe_db_cluster_parameters ANYWHERE, so it could see neither
# IsModifiable nor whether the parameter existed in the group at all.
#
# MEASURED pre-fix against the live cluster pgtsd-demo-aurora-pg / pgtsd-demo-cpg,
# describe-only (the write was intercepted by a local double, AWS was never
# called): `config_file` (IsModifiable=false) -> status modify_failed,
# verify_approval called ONCE (the approval consumed), the write attempted, and
# the reason did not name the parameter. A nonexistent parameter name: identical.
# Post-fix: not_modifiable / unknown_parameter, verify_approval calls 0.
# ---------------------------------------------------------------------------

@patch("mcp_servers.operations.tools.modify_parameter.verify_approval")
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_non_modifiable_parameter_is_refused_before_the_approval_is_consumed(
        mock_rds_for, mock_verify):
    """verify_approval is the ONLY thing that consumes the approval (every
    failure branch in approval_guard returns before the update_item), so
    "not called" is exactly "not consumed"."""
    mock_verify.return_value = {"ok": True}
    mock_rds = _rds(params=[P_FIXED])
    mock_rds_for.return_value = mock_rds
    result = modify_parameter_impl(
        MagicMock(), cluster_id="pgtsd-demo-aurora-pg", parameter_name="config_file",
        value="/tmp/x.conf", approved=True, approval_id="appr-1",
    )
    assert result["status"] == "not_modifiable"
    mock_verify.assert_not_called()
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()
    # Name the parameter: a DBA must not have to guess which one is pinned.
    assert "config_file" == result["parameter"]
    assert "config_file" in result["reason"]
    assert "IsModifiable" in result["reason"]
    assert result["current_value"] == "/rdsdbdata/config/postgresql.conf"


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_non_modifiable_parameter_never_reaches_approval_required(mock_rds_for):
    """The preview leg must refuse too: offering a card for a change AWS will
    reject is how the wasted approval gets minted in the first place."""
    mock_rds_for.return_value = _rds(params=[P_FIXED])
    result = modify_parameter_impl(
        MagicMock(), cluster_id="pgtsd-demo-aurora-pg", parameter_name="config_file",
        value="/tmp/x.conf",
    )
    assert result["status"] == "not_modifiable"
    assert result["apply_type"] == "static"


@patch("mcp_servers.operations.tools.modify_parameter.verify_approval")
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_unknown_parameter_is_refused_before_the_approval_is_consumed(
        mock_rds_for, mock_verify):
    """A name the engine family does not have is accepted by
    modify_db_cluster_parameter_group into a group nothing reads, or rejected
    outright — either way the DBA's approval must not pay for finding out."""
    mock_verify.return_value = {"ok": True}
    mock_rds = _rds(params=[P_OK])
    mock_rds_for.return_value = mock_rds
    result = modify_parameter_impl(
        MagicMock(), cluster_id="pgtsd-demo-aurora-pg",
        parameter_name="not_a_real_parameter", value="1",
        approved=True, approval_id="appr-1",
    )
    assert result["status"] == "unknown_parameter"
    mock_verify.assert_not_called()
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()
    assert result["parameter"] == "not_a_real_parameter"


@patch("mcp_servers.operations.tools.modify_parameter.verify_approval")
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_parameter_scan_failure_is_lookup_failed_not_unknown_parameter(
        mock_rds_for, mock_verify):
    """"the group says this parameter does not exist" and "we could not ask" are
    different answers, and only the first is a safe refusal. Conflating them
    would tell a DBA their parameter name is wrong when the call simply failed."""
    mock_verify.return_value = {"ok": True}
    mock_rds = _rds(pages=RuntimeError("Throttling: arn:aws:rds:x"))
    mock_rds_for.return_value = mock_rds
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="8MB",
        approved=True, approval_id="appr-1",
    )
    assert result["status"] == "lookup_failed"
    mock_verify.assert_not_called()
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()
    blob = " ".join(str(v) for v in result.values())
    for leak in ("Throttling", "arn:aws:rds", "RuntimeError"):
        assert leak not in blob, f"raw exception text leaked: {result}"


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_a_parameter_without_the_field_is_not_declared_non_modifiable(mock_rds_for):
    """Only an explicit False refuses. A response that does not carry
    IsModifiable has not told us the parameter is fixed, and reporting it as
    fixed would be a negative the data cannot support."""
    mock_rds_for.return_value = _rds(params=[
        {"ParameterName": "work_mem", "ApplyType": "dynamic"}])
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="8MB")
    assert result["status"] == "approval_required"


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_parameter_found_on_a_later_page(mock_rds_for):
    """MEASURED 5 pages for pgtsd-demo-cpg (448 parameters), so a single-page
    scan would call almost every real parameter nonexistent."""
    mock_rds = _rds(pages=[
        {"Parameters": [{"ParameterName": "a", "ApplyType": "dynamic"}], "Marker": "m1"},
        {"Parameters": [{"ParameterName": "b", "ApplyType": "dynamic"}], "Marker": "m2"},
        {"Parameters": [P_WORK_MEM]},
    ])
    mock_rds_for.return_value = mock_rds
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="8MB")
    assert result["status"] == "approval_required"
    assert mock_rds.describe_db_cluster_parameters.call_count == 3
    # The CLUSTER form of the API, never the instance form.
    mock_rds.describe_db_parameters.assert_not_called()


# ---------------------------------------------------------------------------
# The ARGUMENTS are a precondition too, and sharing a case-insensitive lookup
# without normalising them was a hole the sharing itself opened: a padded or
# mis-cased name PASSED the new preflight and then the CALLER's raw string was
# handed to modify_db_cluster_parameter_group, after the guard had consumed the
# single-use approval.
#
# MEASURED with verify_approval spied and the write intercepted by a local double
# (AWS never called). Pre-fix, execute leg: " max_connections " and
# MAX_CONNECTIONS both reached the write VERBATIM with verify_approval called
# ONCE, and an empty value reached it as ParameterValue "". Post-fix: the first
# two are sent as 'max_connections' (byte-identical to the clean-name happy path)
# and the empty value is invalid_request with 0 consumes.
#
# Live grounding for adopting the API's spelling (describe-only, ap-northeast-2,
# 2026-07-29): of the 448 / 416 / 424 parameters in pgtsd-demo-cpg,
# default.aurora-postgresql15 and default.aurora-mysql8.0, ZERO differ from their
# own stripped+lowered form and zero collide under the fold. So on Aurora the
# adopted spelling can only ever differ from the caller's when the caller sent
# padding or the wrong case, and folding parameter_name in
# approval_guard._project cannot move the hash of an approval that named a real
# parameter.
# ---------------------------------------------------------------------------

@patch("mcp_servers.operations.tools.modify_parameter.verify_approval")
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_a_padded_name_is_written_in_the_apis_spelling(mock_rds_for, mock_verify):
    """The write, the approval payload and the response all carry the name
    describe_db_cluster_parameters reported, never the caller's raw string."""
    mock_verify.return_value = {"ok": True}
    mock_rds = _rds(params=[P_WORK_MEM])
    mock_rds_for.return_value = mock_rds
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="  work_mem  ",
        value="  8MB  ", parameter_group=CUSTOM_PG, approved=True, approval_id="appr-1")
    assert result["status"] == "modified"
    assert result["parameter"] == "work_mem"
    assert result["value"] == "8MB"
    sent = mock_rds.modify_db_cluster_parameter_group.call_args.kwargs["Parameters"][0]
    assert sent["ParameterName"] == "work_mem"
    assert sent["ParameterValue"] == "8MB"
    # The guard has to be asked about the SAME text that gets written, or the
    # approval is bound to one operation and the write performs another.
    assert mock_verify.call_args.kwargs["payload"] == {
        "parameter_name": "work_mem", "value": "8MB",
        "parameter_group": CUSTOM_PG}


@patch("mcp_servers.operations.tools.modify_parameter.verify_approval")
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_a_case_difference_is_written_in_the_apis_spelling(mock_rds_for, mock_verify):
    """The lookup folds case (it is shared with the INSTANCE tool, where SQL
    Server forces it), so the caller's spelling must not survive into the write:
    matching case-insensitively and then sending MAX_CONNECTIONS moved the burnt
    approval from the preflight to the API instead of removing it."""
    mock_verify.return_value = {"ok": True}
    mock_rds = _rds(params=[P_WORK_MEM])
    mock_rds_for.return_value = mock_rds
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="WORK_MEM", value="8MB",
        parameter_group=CUSTOM_PG, approved=True, approval_id="appr-1")
    assert result["status"] == "modified"
    assert result["parameter"] == "work_mem"
    sent = mock_rds.modify_db_cluster_parameter_group.call_args.kwargs["Parameters"][0]
    assert sent["ParameterName"] == "work_mem"


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_the_approval_card_advertises_the_apis_spelling(mock_rds_for):
    """The preview leg is where the card is minted, so it has to carry the same
    normalised name the execute leg will hash and write."""
    mock_rds_for.return_value = _rds(params=[P_WORK_MEM])
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name=" WORK_MEM ", value=" 8MB ")
    assert result["status"] == "approval_required"
    assert result["parameter"] == "work_mem"
    assert result["value"] == "8MB"


@patch("mcp_servers.operations.tools.modify_parameter.verify_approval")
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_an_empty_value_is_refused_without_consuming_the_approval(
        mock_rds_for, mock_verify):
    """An empty value used to be sent as ParameterValue "" after the consume.
    Clearing a parameter back to the engine default is
    reset_db_cluster_parameter_group, a different operation, so this refuses
    instead of guessing, and refuses before the guard."""
    mock_verify.return_value = {"ok": True}
    mock_rds = _rds()
    mock_rds_for.return_value = mock_rds
    for empty in ("", "   ", None):
        result = modify_parameter_impl(
            MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem",
            value=empty, approved=True, approval_id="appr-1")
        assert result["status"] == "invalid_request", empty
        mock_verify.assert_not_called()
        mock_rds.modify_db_cluster_parameter_group.assert_not_called()


@patch("mcp_servers.operations.tools.modify_parameter.verify_approval")
@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_an_empty_parameter_name_is_refused_without_consuming_the_approval(
        mock_rds_for, mock_verify):
    mock_verify.return_value = {"ok": True}
    mock_rds = _rds()
    mock_rds_for.return_value = mock_rds
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="   ", value="8MB",
        approved=True, approval_id="appr-1")
    assert result["status"] == "invalid_request"
    mock_verify.assert_not_called()
    mock_rds.modify_db_cluster_parameter_group.assert_not_called()


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_a_falsy_but_real_value_is_not_treated_as_empty(mock_rds_for):
    """"0" and "off" are legitimate parameter values. Only whitespace is
    stripped, and only an actually-empty value is refused."""
    mock_rds_for.return_value = _rds(params=[P_WORK_MEM])
    for real in ("0", "off", "OFF"):
        result = modify_parameter_impl(
            MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value=real)
        assert result["status"] == "approval_required", real
        assert result["value"] == real


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_a_name_that_is_wrong_beyond_case_is_still_unknown(mock_rds_for):
    """Case folding must not turn the honest "no such parameter" answer into a
    fuzzy match."""
    mock_rds_for.return_value = _rds(params=[P_WORK_MEM])
    result = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_memory", value="8MB")
    assert result["status"] == "unknown_parameter"

# ===========================================================================
# THE GROUP IS PART OF WHAT WAS APPROVED
# ===========================================================================
# `parameter_group` used to be informational: the projection bound
# {parameter_name, value}, so a cluster re-pointed to a DIFFERENT cluster
# parameter group between approval and execute had the write land on the new group
# while the card named none. Same parameter, same value, wrong target, and nothing
# in the audit record said so. It is now hash-bound and compared pre-consume,
# mirroring modify_rds_instance_params.


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_group_drift_is_refused_before_the_approval_is_consumed(mock_rds_for):
    mock_rds_for.return_value = _rds(params=[P_WORK_MEM])
    consumed = []
    with patch("mcp_servers.operations.tools.modify_parameter.verify_approval",
               side_effect=lambda *a, **k: consumed.append(1) or {"ok": True}):
        result = modify_parameter_impl(
            MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem",
            value="8MB", parameter_group="some-other-group",
            approved=True, approval_id="appr-1")
    assert result["status"] == "state_changed", result
    assert result["parameter_group"] == CUSTOM_PG
    assert result["approved_parameter_group"] == "some-other-group"
    assert consumed == [], "a drift visible before the guard must not cost the approval"
    # And the reason tells the DBA the approval survived, so they re-request rather
    # than assuming it is spent.
    assert "소모하지 않았" in result["reason"], result["reason"]


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_an_omitted_group_does_not_claim_the_group_changed(mock_rds_for):
    """An omitted arg is not evidence of drift, so the sentence says "different
    from", not "changed". Same wording rule as the instance tool."""
    mock_rds_for.return_value = _rds(params=[P_WORK_MEM])
    consumed = []
    with patch("mcp_servers.operations.tools.modify_parameter.verify_approval",
               side_effect=lambda *a, **k: consumed.append(1) or {"ok": True}):
        result = modify_parameter_impl(
            MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem",
            value="8MB", approved=True, approval_id="appr-1")
    assert result["status"] == "state_changed", result
    assert consumed == []
    assert "다릅니다" in result["reason"], result["reason"]


@patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster")
def test_the_preview_reports_the_group_the_approved_call_must_echo(mock_rds_for):
    """The round trip has to be closable from the tool's own output: the card the
    agent registers comes from this response, so the group must be in it."""
    mock_rds_for.return_value = _rds(params=[P_WORK_MEM])
    preview = modify_parameter_impl(
        MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem", value="8MB")
    assert preview["status"] == "approval_required"
    assert preview["parameter_group"] == CUSTOM_PG

    # Echoing it back reaches the write.
    mock_rds = _rds(params=[P_WORK_MEM])
    mock_rds_for.return_value = mock_rds
    with patch("mcp_servers.operations.tools.modify_parameter.verify_approval",
               return_value={"ok": True}):
        result = modify_parameter_impl(
            MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem",
            value="8MB", parameter_group=preview["parameter_group"],
            approved=True, approval_id="appr-1")
    assert result["status"] == "modified", result
    kwargs = mock_rds.modify_db_cluster_parameter_group.call_args.kwargs
    assert kwargs["DBClusterParameterGroupName"] == CUSTOM_PG


def test_the_projection_binds_the_group_and_does_not_fold_its_case():
    """AWS parameter group names are case-sensitive, so folding them would let an
    approval for one group be consumed for another whose name differs only in
    case. parameter_name IS folded, for the opposite reason (the tool adopts the
    API's spelling), and that asymmetry is deliberate."""
    from mcp_servers.shared.approval_guard import canonical_action_hash as h
    base = {"parameter_name": "work_mem", "value": "8MB", "parameter_group": "grp-a"}
    other = dict(base, parameter_group="grp-b")
    upper = dict(base, parameter_group="GRP-A")
    assert h("modify_parameter", base) != h("modify_parameter", other)
    assert h("modify_parameter", base) != h("modify_parameter", upper)
    # The name still folds, and padding on either still collapses.
    assert h("modify_parameter", base) == h(
        "modify_parameter", {"parameter_name": " WORK_MEM ", "value": " 8MB ",
                             "parameter_group": "grp-a"})


def _real_guard_table():
    """(register, execute) closures over an in-memory approvals table using the
    REAL _project and canonical_action_hash, so the register leg and the execute
    leg have to agree for real rather than by mock."""
    import os

    from mcp_servers.operations.tools.request_approval import request_approval_impl
    from mcp_servers.shared import approval_guard

    os.environ.setdefault("APPROVALS_TABLE", "t")
    table = {}

    def register(details):
        with patch("mcp_servers.operations.tools.request_approval.boto3") as b:
            tbl = MagicMock()
            tbl.put_item.side_effect = lambda Item: table.__setitem__(
                Item["approval_id"], dict(Item))
            b.resource.return_value.Table.return_value = tbl
            return request_approval_impl(
                MagicMock(), "prod-pg-1", "modify_parameter", details)

    def verify(approval_id, cluster_id, action_type, payload=None):
        row = table.get(approval_id)
        if not row:
            return {"ok": False, "reason": "not found"}
        if row["payload_hash"] != approval_guard.canonical_action_hash(
                action_type, payload or {}):
            return {"ok": False, "reason": "payload mismatch"}
        if row.get("consumed"):
            return {"ok": False, "reason": "already consumed"}
        row["consumed"] = True
        return {"ok": True}

    def execute(approval_id, group_arg, live_group=CUSTOM_PG):
        with patch("mcp_servers.operations.tools.modify_parameter.rds_client_for_cluster",
                   return_value=_rds(live_group, params=[P_WORK_MEM])), \
             patch("mcp_servers.operations.tools.modify_parameter.verify_approval",
                   side_effect=verify):
            return modify_parameter_impl(
                MagicMock(), cluster_id="prod-pg-1", parameter_name="work_mem",
                value="8MB", parameter_group=group_arg, approved=True,
                approval_id=approval_id)

    return register, execute, table


def test_a_transient_group_drift_leaves_the_card_usable():
    """The point of refusing BEFORE the guard: the approval has to survive so the
    same card still works once the cluster is back on the group the DBA reviewed.
    A post-consume refusal would have destroyed it."""
    register, execute, table = _real_guard_table()
    card = register({"parameter_name": "work_mem", "value": "8MB",
                     "parameter_group": CUSTOM_PG})
    drift = execute(card["approval_id"], CUSTOM_PG, live_group="some-other-group")
    assert drift["status"] == "state_changed", drift
    assert table[card["approval_id"]].get("consumed") is not True

    ok = execute(card["approval_id"], CUSTOM_PG)
    assert ok["status"] == "modified", ok
    assert table[card["approval_id"]]["consumed"] is True
    # ...and only once.
    assert execute(card["approval_id"], CUSTOM_PG)["status"] == "approval_denied"


def test_a_card_minted_without_the_group_fails_closed_without_burning():
    """Adding parameter_group to the projection changed the hash for this action,
    so a card minted before the change cannot verify. It must fail CLOSED and
    leave the approval intact, not consume it: cards live 24h, so this is a
    bounded one-time cost and the DBA can simply re-request."""
    register, execute, table = _real_guard_table()
    legacy = register({"parameter_name": "work_mem", "value": "8MB"})
    resp = execute(legacy["approval_id"], CUSTOM_PG)
    assert resp["status"] == "approval_denied", resp
    assert table[legacy["approval_id"]].get("consumed") is not True


def test_a_card_for_one_group_cannot_be_consumed_for_another():
    """The whole point of binding it. Even when the executing call's arg agrees
    with the LIVE group (so the pre-consume comparison passes), the hash refuses,
    because the card named a different group."""
    register, execute, table = _real_guard_table()
    card = register({"parameter_name": "work_mem", "value": "8MB",
                     "parameter_group": "grp-a"})
    resp = execute(card["approval_id"], "grp-b", live_group="grp-b")
    assert resp["status"] == "approval_denied", resp
    assert table[card["approval_id"]].get("consumed") is not True
