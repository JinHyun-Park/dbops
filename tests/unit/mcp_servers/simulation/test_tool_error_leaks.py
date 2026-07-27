"""No simulation TOOL may interpolate exception text into its response.

The handler-level catch-all was cleaned in test_handler_error_leaks.py, but each
tool's own degrade path had the same shape one level down: a describe failure
was formatted into the `reason` / `data_source` / `reader_note` the agent reads.
An RDS or ElastiCache error carries the hub account id, the platform IAM role
name and the target ARN, and a tool response goes straight into the DBA's agent
transcript.

Diagnostics belong in CloudWatch via the module logger, never in the payload.
These tools all DEGRADE rather than fail, so the assertions also pin that the
degraded verdict itself is unchanged: this was a message-content fix only.
"""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TOOLS = Path(__file__).resolve().parents[4] / "mcp-servers/mcp_servers/simulation/tools"

HUB_ACCOUNT = "999988887777"
PLATFORM_ROLE = "dbops-prod-mcp-simulation-role"
TARGET_ARN = f"arn:aws:rds:ap-northeast-2:{HUB_ACCOUNT}:cluster:super-secret-prod"
SECRET_FAULT = (
    f"User: arn:aws:sts::{HUB_ACCOUNT}:assumed-role/{PLATFORM_ROLE}/s is not authorized "
    f"to perform: rds:DescribeDBClusters on resource: {TARGET_ARN}"
)


def _load(name):
    spec = importlib.util.spec_from_file_location(f"leak_{name}", _TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _boom(*_a, **_k):
    raise RuntimeError(SECRET_FAULT)


def _assert_clean(blob: str, where: str):
    assert SECRET_FAULT not in blob, f"raw fault leaked into {where}"
    assert HUB_ACCOUNT not in blob, f"hub account id leaked into {where}"
    assert PLATFORM_ROLE not in blob, f"platform role name leaked into {where}"
    assert TARGET_ARN not in blob, f"target ARN leaked into {where}"
    assert "assumed-role" not in blob, f"role session leaked into {where}"
    assert "not authorized" not in blob, f"fault text leaked into {where}"


def test_elasticache_resize_describe_failure_is_clean():
    mod = _load("elasticache_scaling_simulation")
    mod.lookup_cluster = lambda cid: {
        "resource_name": "my-redis", "region": "ap-northeast-2",
        "resource_details": {"engine": "redis"},
    }
    mod.client_for_cluster = _boom
    r = mod.simulate_elasticache_node_resize_impl(
        None, cluster_id="my-redis", new_node_type="cache.r7g.large")
    _assert_clean(str(r), "simulate_elasticache_node_resize")
    assert r["status"] == "error"          # verdict unchanged
    assert "조회할 수 없습니다" in r["reason"]  # still actionable


@pytest.mark.parametrize("failing", ["describe_db_clusters", "describe_all_parameters"])
def test_parameter_simulation_degrade_is_clean(failing):
    """Both degrade points: the cluster describe and the parameter pagination."""
    mod = _load("parameter_simulation")
    if failing == "describe_db_clusters":
        mod.rds_client_for_cluster = _boom
    else:
        mod.rds_client_for_cluster = lambda cid: MagicMock(
            describe_db_clusters=MagicMock(
                return_value={"DBClusters": [{"DBClusterParameterGroup": "custom-pg15"}]}))
        mod.describe_all_parameters = _boom

    r = mod.simulate_parameter_change_impl(None, "prod-pg", "work_mem", "64MB")
    _assert_clean(str(r), f"simulate_parameter_change ({failing})")
    # Still degrades to the static catalog, and still SAYS it degraded.
    assert r["data_source"] == "static fallback (live describe unavailable)"


def test_upgrade_impact_reader_note_is_clean():
    mod = _load("upgrade_impact")
    mod.rds_client_for_cluster = _boom
    readers, note = mod._resolve_reader_count("prod-pg")
    _assert_clean(note, "upgrade_impact reader_note")
    assert readers == 0                      # degrade behaviour unchanged
    assert note, "the note must still tell the caller the count was assumed"


def test_upgrade_plan_reader_note_is_clean():
    mod = _load("upgrade_plan")
    mod.rds_client_for_cluster = _boom
    readers, note = mod._resolve_reader_count("prod-pg")
    _assert_clean(note, "upgrade_plan reader_note")
    assert readers == 0
    assert "0으로 가정" in note


def test_scaling_simulation_degrade_reason_is_fully_static():
    """This one used type(e).__name__, which leaks no identifiers but is also
    useless to a DBA. The reason is now static and the class name is logged, so
    no part of the exception object reaches the transcript."""
    mod = _load("scaling_simulation")
    mod.rds_client_for_cluster = _boom
    r = mod.simulate_scaling_impl(None, "prod-pg", new_min_acu=2, new_max_acu=8)
    blob = str(r)
    _assert_clean(blob, "simulate_scaling degraded result")
    assert "RuntimeError" not in blob, "exception class name reached the response"
    assert "조회 실패" in blob, "the degraded verdict must still be stated"


def test_observed_acu_note_is_static():
    """The ACU note is a separate response field with the same old shape."""
    mod = _load("scaling_simulation")
    mod.client_for_cluster = _boom
    acu, note = mod._observed_avg_acu("prod-pg")
    _assert_clean(note, "observed ACU note")
    assert acu is None
    assert "RuntimeError" not in note
