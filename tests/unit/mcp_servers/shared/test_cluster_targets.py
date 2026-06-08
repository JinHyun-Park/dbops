"""Tests for cluster_targets — hub-spoke target resolution for control-plane
RDS operations."""

from unittest.mock import MagicMock, patch

import mcp_servers.shared.cluster_targets as ct


@patch.object(ct, "boto3")
def test_session_for_local_when_no_role(mock_boto3):
    """No role_arn → a plain region-scoped session, no assume_role."""
    ct.session_for(region="ap-northeast-2")
    mock_boto3.session.Session.assert_called_once_with(region_name="ap-northeast-2")
    mock_boto3.client.assert_not_called()  # no sts


@patch.object(ct, "boto3")
def test_session_for_assumes_role_when_given(mock_boto3):
    """role_arn present → assume_role and build a session from the temp creds."""
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AKIA", "SecretAccessKey": "secret", "SessionToken": "tok",
        }
    }
    mock_boto3.client.return_value = sts
    ct.session_for(region="us-east-1", role_arn="arn:aws:iam::222:role/dbops-spoke")
    mock_boto3.client.assert_called_once_with("sts")
    assert sts.assume_role.call_args.kwargs["RoleArn"] == "arn:aws:iam::222:role/dbops-spoke"
    kwargs = mock_boto3.session.Session.call_args.kwargs
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["aws_access_key_id"] == "AKIA"
    assert kwargs["aws_session_token"] == "tok"


def test_rds_client_for_cluster_uses_registry_region_and_role():
    """rds_client_for_cluster resolves region + spoke_role_arn from the
    registry and routes the RDS client through session_for."""
    row = {"region": "eu-west-1", "spoke_role_arn": "arn:aws:iam::333:role/spoke"}
    with patch.object(ct, "lookup_cluster", return_value=row) as mock_lookup, \
         patch.object(ct, "session_for") as mock_session_for:
        rds = MagicMock()
        mock_session_for.return_value.client.return_value = rds
        out = ct.rds_client_for_cluster("prod-pg-1")
        mock_lookup.assert_called_once_with("prod-pg-1")
        mock_session_for.assert_called_once_with("eu-west-1", "arn:aws:iam::333:role/spoke")
        mock_session_for.return_value.client.assert_called_once_with("rds")
        assert out is rds


def test_rds_client_for_unregistered_cluster_falls_back_local():
    """Unknown cluster → empty registry row → local session (no region/role)."""
    with patch.object(ct, "lookup_cluster", return_value={}), \
         patch.object(ct, "session_for") as mock_session_for:
        ct.rds_client_for_cluster("ghost")
        mock_session_for.assert_called_once_with("", "")
