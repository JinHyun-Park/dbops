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


def test_created_ms_orders_mixed_formats_chronologically():
    """ms-epoch 문자열(MCP request_approval)과 ISO(UI POST)가 섞여도
    시간순으로 정렬돼야 한다 — 문자열 비교는 '2026...' > '1781...'이라
    UI발 행이 무조건 최신으로 보이는 버그가 있었다."""
    older_epoch = {"created_at": "1781069757421"}   # 2026-06-10T05:35:57Z
    newer_iso = {"created_at": "2026-06-10T06:42:08"}
    oldest_iso = {"created_at": "2026-06-09T01:00:00"}
    rows = sorted(
        [newer_iso, older_epoch, oldest_iso], key=handler._created_ms, reverse=True
    )
    assert rows == [newer_iso, older_epoch, oldest_iso]


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals", "CLUSTERS_TABLE": "clusters"})
def test_put_on_already_consumed_returns_409():
    """이미 consumed/rejected된 행은 PUT approve로 되살릴 수 없다 — 없으면
    guard의 consume-on-use replay 방어를 API에서 우회할 수 있다(Codex)."""
    from botocore.exceptions import ClientError
    row = dict(_ROW)
    row["approval_status"] = "consumed"
    mock_boto3, approvals_table, _ = _boto3_with([row])
    approvals_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    with patch.object(handler, "boto3", mock_boto3):
        resp = handler.lambda_handler(_event("PUT", approval_id="aid-eda-1", body={"action": "approve"}), None)
    assert resp["statusCode"] == 409
    assert json.loads(resp["body"])["error"] == "already_resolved"


@patch.dict("os.environ", {"APPROVALS_TABLE": "approvals", "CLUSTERS_TABLE": "clusters"})
def test_post_rejects_non_enable_data_api_write_action():
    """UI POST로는 enable_data_api 외 쓰기 승인을 만들 수 없다 — payload_hash
    없는 쓰기 승인 생성을 봉쇄(Codex 감사 P0)."""
    mock_boto3, approvals_table, _ = _boto3_with([])
    with patch.object(handler, "boto3", mock_boto3):
        resp = handler.lambda_handler(
            _event("POST", body={"cluster_id": "x", "action_type": "execute_sql",
                                 "action_details": {"sql": "DROP TABLE t"}}),
            None,
        )
    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "unsupported_action_type"
    approvals_table.put_item.assert_not_called()
