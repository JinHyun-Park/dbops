"""Tests for GET /api/reports/{id}/html — presigned URL for the HTML twin."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

_PATH = Path(__file__).resolve().parents[3] / "api" / "reports" / "handler.py"
_spec = importlib.util.spec_from_file_location("reports_handler", _PATH)
handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(handler)


def _event(report_id="42"):
    return {
        "httpMethod": "GET",
        "requestContext": {"http": {"method": "GET"}},
        "rawPath": f"/api/reports/{report_id}/html",
        "pathParameters": {"id": report_id},
        "queryStringParameters": {},
    }


def _rds_row(s3_key="reports/2026/06/24/cluster-1/daily.json"):
    """Minimal RDS Data API response for a single report row."""
    return {
        "columnMetadata": [
            {"name": "id", "typeName": "int8"},
            {"name": "s3_key", "typeName": "text"},
        ],
        "records": [
            [
                {"longValue": 42},
                {"stringValue": s3_key},
            ]
        ],
    }


def _not_found_rds():
    return {"columnMetadata": [], "records": []}


def _s3_client_error(code="404"):
    error = ClientError(
        {"Error": {"Code": code, "Message": "Not Found"}},
        "HeadObject",
    )
    return error


@patch.dict(
    "os.environ",
    {
        "CACHE_DB_CLUSTER_ARN": "arn:aws:rds:ap-northeast-2:123:cluster:test",
        "CACHE_DB_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123:secret:test",
        "CACHE_DB_NAME": "dbops",
        "ARCHIVE_BUCKET": "dbops-dev-archive-123456789012",
    },
)
@patch.object(handler, "boto3")
def test_html_presigned_url_when_html_exists(mock_boto3):
    """HTML twin present -> 200 with presigned url."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_row(
        "reports/2026/06/24/cluster-1/daily.json"
    )
    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {}
    mock_s3.generate_presigned_url.return_value = (
        "https://s3.presigned.example.com/reports/...?X-Amz-Signature=abc"
    )
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_rds if svc == "rds-data" else mock_s3
    )

    res = handler.lambda_handler(_event("42"), None)

    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert "url" in body
    assert body["url"].startswith("https://")
    # head_object called with the .html key
    mock_s3.head_object.assert_called_once_with(
        Bucket="dbops-dev-archive-123456789012",
        Key="reports/2026/06/24/cluster-1/daily.html",
    )
    # generate_presigned_url called with get_object + correct key
    mock_s3.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={
            "Bucket": "dbops-dev-archive-123456789012",
            "Key": "reports/2026/06/24/cluster-1/daily.html",
        },
        ExpiresIn=300,
    )


@patch.dict(
    "os.environ",
    {
        "CACHE_DB_CLUSTER_ARN": "arn:aws:rds:ap-northeast-2:123:cluster:test",
        "CACHE_DB_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123:secret:test",
        "CACHE_DB_NAME": "dbops",
        "ARCHIVE_BUCKET": "dbops-dev-archive-123456789012",
    },
)
@patch.object(handler, "boto3")
def test_html_404_when_html_absent(mock_boto3):
    """HTML twin not in S3 -> 404 with Korean note."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _rds_row(
        "reports/2026/06/24/cluster-1/daily.json"
    )
    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = _s3_client_error("404")
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_rds if svc == "rds-data" else mock_s3
    )

    res = handler.lambda_handler(_event("42"), None)

    assert res["statusCode"] == 404
    body = json.loads(res["body"])
    assert "HTML" in body.get("error", "") or "html" in body.get("error", "").lower()


@patch.dict(
    "os.environ",
    {
        "CACHE_DB_CLUSTER_ARN": "arn:aws:rds:ap-northeast-2:123:cluster:test",
        "CACHE_DB_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123:secret:test",
        "CACHE_DB_NAME": "dbops",
        "ARCHIVE_BUCKET": "dbops-dev-archive-123456789012",
    },
)
@patch.object(handler, "boto3")
def test_html_404_when_report_not_found(mock_boto3):
    """Report row not in DB -> 404."""
    mock_rds = MagicMock()
    mock_rds.execute_statement.return_value = _not_found_rds()
    mock_s3 = MagicMock()
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_rds if svc == "rds-data" else mock_s3
    )

    res = handler.lambda_handler(_event("99"), None)

    assert res["statusCode"] == 404


@patch.dict(
    "os.environ",
    {
        "CACHE_DB_CLUSTER_ARN": "arn:aws:rds:ap-northeast-2:123:cluster:test",
        "CACHE_DB_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123:secret:test",
        "CACHE_DB_NAME": "dbops",
        "ARCHIVE_BUCKET": "dbops-dev-archive-123456789012",
    },
)
@patch.object(handler, "boto3")
def test_html_404_when_s3_key_missing(mock_boto3):
    """Report row has no s3_key -> 404 with Korean note about pre-HTML report."""
    mock_rds = MagicMock()
    # s3_key is null
    mock_rds.execute_statement.return_value = {
        "columnMetadata": [
            {"name": "id", "typeName": "int8"},
            {"name": "s3_key", "typeName": "text"},
        ],
        "records": [
            [
                {"longValue": 42},
                {"isNull": True},
            ]
        ],
    }
    mock_s3 = MagicMock()
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_rds if svc == "rds-data" else mock_s3
    )

    res = handler.lambda_handler(_event("42"), None)

    assert res["statusCode"] == 404
    body = json.loads(res["body"])
    assert "이전" in body.get("error", "") or "HTML" in body.get("error", "")


@patch.dict(
    "os.environ",
    {
        "CACHE_DB_CLUSTER_ARN": "arn:aws:rds:ap-northeast-2:123:cluster:test",
        "CACHE_DB_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:123:secret:test",
        "CACHE_DB_NAME": "dbops",
        "ARCHIVE_BUCKET": "dbops-dev-archive-123456789012",
    },
)
@patch.object(handler, "boto3")
def test_html_400_for_non_numeric_id(mock_boto3):
    """Non-numeric id -> 400 (re-uses existing guard)."""
    mock_rds = MagicMock()
    mock_s3 = MagicMock()
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_rds if svc == "rds-data" else mock_s3
    )

    res = handler.lambda_handler(_event("not-a-number"), None)

    assert res["statusCode"] == 400
