"""Tests for the /param-diff sub-view (P2-⑨ default vs current parameter diff).

_param_diff(cluster_id) diffs a relational cluster's LIVE DB cluster parameter
group against the family's DEFAULT parameter group (`default.<family>`),
returning only the parameters whose current value differs from the default.
Both sides are fetched via describe_db_cluster_parameters so they share the
same materialized representation (the engine-default catalog returned empty
values → false positives; that path is gone).
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

_DASHBOARD_DIR = Path(__file__).resolve().parents[3] / "api" / "dashboard"
sys.path.insert(0, str(_DASHBOARD_DIR))

_PATH = _DASHBOARD_DIR / "handler.py"
_spec = importlib.util.spec_from_file_location("dashboard_handler_paramdiff", _PATH)
handler = importlib.util.module_from_spec(_spec)

os.environ.setdefault("CLUSTERS_TABLE", "clusters-stub")
os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:123:cluster:cache")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:123:secret:cache")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

_spec.loader.exec_module(handler)

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("CLUSTERS_TABLE", "clusters-stub")


def _describe_db_clusters_resp(pg_name="prod-pg-params"):
    return {"DBClusters": [{"DBClusterParameterGroup": pg_name}]}


def _describe_pg_groups_resp(family="aurora-postgresql15"):
    return {"DBClusterParameterGroups": [{"DBParameterGroupFamily": family}]}


def _params_dispatch(pages_by_group):
    """Build a describe_db_cluster_parameters side_effect that dispatches on
    the DBClusterParameterGroupName kwarg. pages_by_group maps a group name to
    a list of full response pages (each a dict with Parameters + optional
    Marker); successive calls for the same group walk the pages, mirroring the
    handler's Marker pagination."""
    counters = {}

    def _side_effect(**kwargs):
        g = kwargs["DBClusterParameterGroupName"]
        pages = pages_by_group[g]
        i = counters.get(g, 0)
        counters[g] = i + 1
        return pages[i]

    return _side_effect


def test_param_diff_dynamodb_not_applicable_no_rds_call(monkeypatch):
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "dynamodb")

    mock_rds = MagicMock()
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._param_diff("ddb-abc123")

    assert result["available"] is False
    assert result.get("not_applicable") is True
    assert result.get("engine_family") == "dynamodb"
    mock_rds.describe_db_clusters.assert_not_called()


def test_param_diff_registry_unavailable_fail_closed(monkeypatch):
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: None)

    mock_session = MagicMock()
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._param_diff("some-cluster")

    assert result["available"] is False
    assert result.get("registry_unavailable") is True
    mock_session.client.assert_not_called()


def test_param_diff_no_parameter_group_unavailable(monkeypatch):
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = {"DBClusters": [{"DBClusterParameterGroup": None}]}
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._param_diff("prod-pg")

    assert result["available"] is False
    mock_rds.describe_db_cluster_parameter_groups.assert_not_called()


def test_param_diff_relational_filters_to_differing_only(monkeypatch):
    """Only params whose current value differs from the DEFAULT GROUP value —
    and is actually set — survive. Unset (empty ParameterValue) and matching-
    default params are excluded. The matching-default case (random_page_cost)
    is the false-positive regression: the old engine-default catalog reported
    it blank and flagged it as changed; the default group reports the same
    materialized value, so it is correctly filtered."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = _describe_db_clusters_resp()
    mock_rds.describe_db_cluster_parameter_groups.return_value = _describe_pg_groups_resp()
    mock_rds.describe_db_cluster_parameters.side_effect = _params_dispatch({
        "prod-pg-params": [{
            "Parameters": [
                {"ParameterName": "work_mem", "ParameterValue": "16384", "Source": "user", "ApplyType": "dynamic"},
                {"ParameterName": "max_connections", "ParameterValue": "5000", "Source": "user", "ApplyType": "static"},
                {"ParameterName": "shared_buffers", "ParameterValue": "", "Source": "engine-default", "ApplyType": "static"},
                {"ParameterName": "random_page_cost", "ParameterValue": "4", "Source": "engine-default", "ApplyType": "dynamic"},
            ]
            # No Marker → single page.
        }],
        "default.aurora-postgresql15": [{
            "Parameters": [
                {"ParameterName": "work_mem", "ParameterValue": "4096"},
                {"ParameterName": "max_connections", "ParameterValue": "100"},
                {"ParameterName": "shared_buffers", "ParameterValue": "16384"},
                {"ParameterName": "random_page_cost", "ParameterValue": "4"},
            ]
        }],
    })
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._param_diff("prod-pg")

    assert result["available"] is True
    assert result["parameter_group"] == "prod-pg-params"
    assert result["family"] == "aurora-postgresql15"
    assert result["total_params"] == 4
    names = {d["name"] for d in result["diffs"]}
    # work_mem and max_connections differ; shared_buffers is unset (skip);
    # random_page_cost matches the default group value (skip — regression).
    assert names == {"work_mem", "max_connections"}
    assert result["diff_count"] == 2
    wm = next(d for d in result["diffs"] if d["name"] == "work_mem")
    assert wm["current"] == "16384"
    assert wm["default"] == "4096"  # default field = default GROUP's value
    assert wm["apply_type"] == "dynamic"
    assert "differs" not in wm  # diffs subset keeps the legacy shape
    mc = next(d for d in result["diffs"] if d["name"] == "max_connections")
    assert mc["apply_type"] == "static"
    # The buggy engine-default catalog must never be consulted again.
    mock_rds.describe_engine_default_cluster_parameters.assert_not_called()

    # `params` = ALL set params annotated with `differs`. shared_buffers is
    # unset (empty current) so it is excluded; random_page_cost equals the
    # default group value so it appears with differs=False and NOT in diffs.
    pnames = {p["name"] for p in result["params"]}
    assert pnames == {"work_mem", "max_connections", "random_page_cost"}
    assert "shared_buffers" not in pnames
    rpc = next(p for p in result["params"] if p["name"] == "random_page_cost")
    assert rpc["differs"] is False
    assert rpc["current"] == "4"
    assert rpc["name"] not in names  # not surfaced in the diffs subset
    wm_p = next(p for p in result["params"] if p["name"] == "work_mem")
    assert wm_p["differs"] is True
    # params is sorted by name
    assert [p["name"] for p in result["params"]] == sorted(pnames)


def test_param_diff_paginates_current_params_with_marker_hang_guard(monkeypatch):
    """The current group is paginated via Marker. A MagicMock (non-str) Marker
    on the last page must terminate the loop instead of hanging forever — this
    is exactly what a naive mock in a test (or a stray boto3 response shape)
    would otherwise trigger."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = _describe_db_clusters_resp()
    mock_rds.describe_db_cluster_parameter_groups.return_value = _describe_pg_groups_resp()
    mock_rds.describe_db_cluster_parameters.side_effect = _params_dispatch({
        "prod-pg-params": [
            {
                "Parameters": [
                    {"ParameterName": "work_mem", "ParameterValue": "16384", "Source": "user", "ApplyType": "dynamic"},
                ],
                "Marker": "page2",
            },
            {
                "Parameters": [
                    {"ParameterName": "max_connections", "ParameterValue": "5000", "Source": "user", "ApplyType": "static"},
                ],
                # Non-str Marker (e.g. an un-set MagicMock attribute) must stop pagination.
                "Marker": MagicMock(),
            },
        ],
        "default.aurora-postgresql15": [{
            "Parameters": [
                {"ParameterName": "work_mem", "ParameterValue": "4096"},
                {"ParameterName": "max_connections", "ParameterValue": "100"},
            ]
        }],
    })
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._param_diff("prod-pg")

    assert result["available"] is True
    # 2 current-group pages + 1 default-group page.
    assert mock_rds.describe_db_cluster_parameters.call_count == 3
    assert result["total_params"] == 2
    assert {d["name"] for d in result["diffs"]} == {"work_mem", "max_connections"}


def test_param_diff_paginates_default_group_with_marker_hang_guard(monkeypatch):
    """The default group is fetched with the same paginated describe. A non-str
    Marker on its last page must also terminate the loop."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = _describe_db_clusters_resp()
    mock_rds.describe_db_cluster_parameter_groups.return_value = _describe_pg_groups_resp()
    mock_rds.describe_db_cluster_parameters.side_effect = _params_dispatch({
        "prod-pg-params": [{
            "Parameters": [
                {"ParameterName": "work_mem", "ParameterValue": "16384", "Source": "user", "ApplyType": "dynamic"},
            ]
        }],
        "default.aurora-postgresql15": [
            {
                "Parameters": [{"ParameterName": "work_mem", "ParameterValue": "4096"}],
                "Marker": "page2",
            },
            {
                "Parameters": [{"ParameterName": "unrelated_param", "ParameterValue": "x"}],
                "Marker": MagicMock(),
            },
        ],
    })
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._param_diff("prod-pg")

    assert result["available"] is True
    # 1 current-group page + 2 default-group pages.
    assert mock_rds.describe_db_cluster_parameters.call_count == 3
    assert result["diffs"] == [
        {"name": "work_mem", "current": "16384", "default": "4096", "source": "user", "apply_type": "dynamic"}
    ]


def test_param_diff_no_raw_boto_leak_on_error(monkeypatch):
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.side_effect = RuntimeError(
        "botocore.exceptions.ClientError: AccessDenied raw secret leak"
    )
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._param_diff("prod-pg")

    assert result["available"] is False
    assert "raw secret leak" not in str(result)
    assert "AccessDenied" not in str(result)


def test_param_diff_default_group_describe_failure_available_false(monkeypatch):
    """If describing default.<family> raises, the outer try/except must return
    the friendly available:false — NOT fall back to the engine-default
    catalog (the removed buggy baseline)."""
    monkeypatch.setattr(handler, "_registry_engine", lambda cid: "aurora-postgresql")

    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = _describe_db_clusters_resp()
    mock_rds.describe_db_cluster_parameter_groups.return_value = _describe_pg_groups_resp()

    def _describe(**kwargs):
        if kwargs["DBClusterParameterGroupName"] == "default.aurora-postgresql15":
            raise RuntimeError("DBParameterGroupNotFound raw leak")
        return {"Parameters": [
            {"ParameterName": "work_mem", "ParameterValue": "16384", "Source": "user", "ApplyType": "dynamic"},
        ]}

    mock_rds.describe_db_cluster_parameters.side_effect = _describe
    mock_session = MagicMock()
    mock_session.client.return_value = mock_rds
    monkeypatch.setattr(handler, "_cluster_session", lambda cid="", row=None: mock_session)

    result = handler._param_diff("prod-pg")

    assert result["available"] is False
    assert result["diffs"] == []
    assert "raw leak" not in str(result)
    mock_rds.describe_engine_default_cluster_parameters.assert_not_called()
