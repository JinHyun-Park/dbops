"""enable_data_api — 승인-즉시-실행 액션 테스트.

다른 쓰기 액션과 달리 replay(에이전트 재호출)가 없다: DBA가 PUT approve를
누르는 순간 approvals 핸들러가 레지스트리에서 cluster_arn을 찾아
rds:EnableHttpEndpoint를 직접 호출하고 행을 consumed로 마감한다.
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_PATH = Path(__file__).resolve().parents[3] / "api" / "approvals" / "handler.py"
_spec = importlib.util.spec_from_file_location("approvals_handler_eda", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _event(method, path="/api/approvals", body=None, approval_id=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "pathParameters": {"id": approval_id} if approval_id else {},
        "queryStringParameters": {},
        "body": json.dumps(body or {}),
    }


_ROW = {
    "approval_id": "aid-eda-1",
    "created_at": "2026-06-10T00:00:00",
    "approval_status": "pending",
    "cluster_id": "pgtsd-demo-aurora-pg",
    "action_type": "enable_data_api",
}


def _boto3_with(approvals_rows, registry_item=None, rds_client=None):
    """handler.boto3 대체 — DDB resource(approvals/clusters 테이블)와
    rds client를 한 번에 모킹한다."""
    mock = MagicMock()
    approvals_table = MagicMock()
    approvals_table.scan.return_value = {"Items": approvals_rows}
    clusters_table = MagicMock()
    clusters_table.get_item.return_value = {"Item": registry_item or {}}

    def _table(name):
        return clusters_table if name == "clusters" else approvals_table

    mock.resource.return_value.Table.side_effect = _table
    mock.client.return_value = rds_client or MagicMock()
    return mock, approvals_table, clusters_table


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals", "CLUSTERS_TABLE": "clusters"})
def test_approve_executes_enable_and_consumes():
    rds = MagicMock()
    rds.enable_http_endpoint.return_value = {"HttpEndpointEnabled": True}
    mock_boto3, approvals_table, _ = _boto3_with(
        [dict(_ROW)],
        registry_item={"cluster_id": "pgtsd-demo-aurora-pg", "cluster_arn": "arn:aws:rds:ap-northeast-2:1:cluster:pgtsd-demo-aurora-pg"},
        rds_client=rds,
    )
    with patch.object(handler, "boto3", mock_boto3):
        resp = handler.lambda_handler(_event("PUT", approval_id="aid-eda-1", body={"action": "approve"}), None)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["execution"]["ok"] is True
    rds.enable_http_endpoint.assert_called_once_with(
        ResourceArn="arn:aws:rds:ap-northeast-2:1:cluster:pgtsd-demo-aurora-pg"
    )
    # 두 번째 update_item이 행을 consumed로 마감해야 한다 (재사용 불가).
    update_calls = approvals_table.update_item.call_args_list
    assert len(update_calls) == 2
    assert update_calls[1].kwargs["ExpressionAttributeValues"][":c"] == "consumed"


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals", "CLUSTERS_TABLE": "clusters"})
def test_approve_execution_failure_returns_502_and_records_error():
    rds = MagicMock()
    rds.enable_http_endpoint.side_effect = Exception("AccessDenied: nope")
    mock_boto3, approvals_table, _ = _boto3_with(
        [dict(_ROW)],
        registry_item={"cluster_id": "pgtsd-demo-aurora-pg", "cluster_arn": "arn:x"},
        rds_client=rds,
    )
    with patch.object(handler, "boto3", mock_boto3):
        resp = handler.lambda_handler(_event("PUT", approval_id="aid-eda-1", body={"action": "approve"}), None)

    assert resp["statusCode"] == 502
    body = json.loads(resp["body"])
    assert body["execution"]["ok"] is False
    # 실패 시 consumed가 아니라 execution_error만 기록 — 재승인이 재시도 경로.
    update_calls = approvals_table.update_item.call_args_list
    assert len(update_calls) == 2
    assert ":e" in update_calls[1].kwargs["ExpressionAttributeValues"]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals", "CLUSTERS_TABLE": "clusters"})
def test_approve_without_registry_arn_fails_closed():
    rds = MagicMock()
    mock_boto3, _, _ = _boto3_with([dict(_ROW)], registry_item={}, rds_client=rds)
    with patch.object(handler, "boto3", mock_boto3):
        resp = handler.lambda_handler(_event("PUT", approval_id="aid-eda-1", body={"action": "approve"}), None)

    assert resp["statusCode"] == 502
    rds.enable_http_endpoint.assert_not_called()


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals", "CLUSTERS_TABLE": "clusters"})
def test_reject_does_not_execute():
    rds = MagicMock()
    mock_boto3, _, _ = _boto3_with(
        [dict(_ROW)],
        registry_item={"cluster_arn": "arn:x"},
        rds_client=rds,
    )
    with patch.object(handler, "boto3", mock_boto3):
        resp = handler.lambda_handler(_event("PUT", approval_id="aid-eda-1", body={"action": "reject"}), None)

    assert resp["statusCode"] == 200
    rds.enable_http_endpoint.assert_not_called()


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals", "CLUSTERS_TABLE": "clusters"})
def test_post_dedupes_pending_request():
    """같은 클러스터의 pending enable_data_api가 있으면 새 행을 만들지 않고
    기존 행을 200으로 돌려준다 (버튼 더블클릭·재방문 멱등성)."""
    mock_boto3, approvals_table, _ = _boto3_with([dict(_ROW)])
    with patch.object(handler, "boto3", mock_boto3):
        resp = handler.lambda_handler(
            _event("POST", body={
                "cluster_id": "pgtsd-demo-aurora-pg",
                "action_type": "enable_data_api",
                "action_details": {"cluster_id": "pgtsd-demo-aurora-pg"},
            }),
            None,
        )
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["approval_id"] == "aid-eda-1"
    approvals_table.put_item.assert_not_called()


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals", "CLUSTERS_TABLE": "clusters"})
def test_post_creates_new_request_when_none_pending():
    mock_boto3, approvals_table, _ = _boto3_with([])
    with patch.object(handler, "boto3", mock_boto3):
        resp = handler.lambda_handler(
            _event("POST", body={
                "cluster_id": "pgtsd-demo-aurora-pg",
                "action_type": "enable_data_api",
                "action_details": {"cluster_id": "pgtsd-demo-aurora-pg"},
            }),
            None,
        )
    assert resp["statusCode"] == 201
    put = approvals_table.put_item.call_args.kwargs["Item"]
    assert put["action_type"] == "enable_data_api"
    assert put["approval_status"] == "pending"
