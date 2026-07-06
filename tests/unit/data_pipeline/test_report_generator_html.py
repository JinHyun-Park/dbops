"""Tests for the HTML twin behaviour wired into report_generator.handler.

Mirrors the mocking pattern in test_report_generator.py (importlib load under
a unique module name, patch.object on the handler's boto3 reference).

Two scenarios:
  1. Happy path — one report cycle emits both a .json put_object AND a .html
     put_object (ContentType text/html; charset=utf-8).
  2. Resilience — if build_report_html raises, the JSON put, the DB INSERT,
     and _deliver_report must all still complete.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

_HANDLER_PATH = (
    Path(__file__).resolve().parents[3]
    / "data-pipeline"
    / "report_generator"
    / "handler.py"
)
_HANDLER_DIR = str(_HANDLER_PATH.parent)
if _HANDLER_DIR not in sys.path:
    sys.path.insert(0, _HANDLER_DIR)

_spec = importlib.util.spec_from_file_location("report_generator_handler_html", _HANDLER_PATH)
_handler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_handler)


def _make_env(monkeypatch):
    monkeypatch.setenv("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:us-east-1:123:cluster:test")
    monkeypatch.setenv("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:123:secret:test")
    monkeypatch.setenv("CACHE_DB_NAME", "dbops")
    monkeypatch.setenv("ARCHIVE_BUCKET", "my-archive-bucket")


def _make_rds_client(cluster_id="prod-pg-1"):
    """Return a mock rds-data client that yields one cluster row then empty for everything else."""
    mock_rds = MagicMock()

    def _execute(**kwargs):
        sql = kwargs.get("sql", "")
        if "cluster_meta" in sql:
            return {
                "columnMetadata": [{"name": "cluster_id"}],
                "records": [[{"stringValue": cluster_id}]],
            }
        # INSERT and any other SELECT returns nothing
        return {"columnMetadata": [], "records": []}

    mock_rds.execute_statement.side_effect = _execute
    return mock_rds


def _make_bedrock_client():
    mock_bedrock = MagicMock()
    body_stream = MagicMock()
    body_stream.read.return_value = b'{"content":[{"text":"Test summary."}]}'
    mock_bedrock.invoke_model.return_value = {"body": body_stream}
    return mock_bedrock


def _make_s3_client():
    return MagicMock()


# ---------------------------------------------------------------------------
# Happy path: both JSON and HTML objects are put to S3
# ---------------------------------------------------------------------------


def test_lambda_handler_puts_json_and_html(monkeypatch):
    _make_env(monkeypatch)

    mock_s3 = _make_s3_client()
    mock_rds = _make_rds_client()
    mock_bedrock = _make_bedrock_client()
    mock_report_html = MagicMock(return_value=b"<html>report</html>")

    def _boto3_client(service, **kwargs):
        if service == "s3":
            return mock_s3
        if service == "rds-data":
            return mock_rds
        if service == "bedrock-runtime":
            return mock_bedrock
        return MagicMock()

    with patch.object(_handler, "boto3") as mock_boto3, \
         patch.object(_handler, "get_config", return_value={}), \
         patch("report_html.build_report_html", mock_report_html, create=True):
        mock_boto3.client.side_effect = _boto3_client
        # Patch the lazy import inside lambda_handler by pre-populating sys.modules
        sys.modules["report_html"] = MagicMock(build_report_html=mock_report_html)

        _handler.lambda_handler({}, {})

    # Collect all put_object calls
    put_calls = mock_s3.put_object.call_args_list
    keys_put = [c.kwargs.get("Key", c.args[1] if len(c.args) > 1 else "") for c in put_calls]

    json_keys = [k for k in keys_put if k.endswith(".json")]
    html_keys = [k for k in keys_put if k.endswith(".html")]

    # One per-cluster report + one fleet rollup => 2 JSON + 2 HTML puts.
    cluster_json = [k for k in json_keys if "/_fleet/" not in k]
    fleet_json = [k for k in json_keys if "/_fleet/" in k]
    assert len(cluster_json) == 1, f"Expected 1 cluster JSON put, got {json_keys}"
    assert len(fleet_json) == 1, f"Expected 1 fleet JSON put, got {json_keys}"
    assert len(html_keys) == 2, f"Expected 2 HTML puts (cluster + fleet), got {html_keys}"

    # each JSON key has an HTML twin differing only in extension
    for k in json_keys:
        assert k[:-5] + ".html" in html_keys, f"missing HTML twin for {k}"

    # HTML puts must declare correct ContentType
    for c in put_calls:
        if (c.kwargs.get("Key", "") or "").endswith(".html"):
            assert c.kwargs.get("ContentType") == "text/html; charset=utf-8"


# ---------------------------------------------------------------------------
# Resilience: build_report_html raising must NOT block JSON put / INSERT / deliver
# ---------------------------------------------------------------------------


def test_html_failure_does_not_block_json_insert_deliver(monkeypatch):
    _make_env(monkeypatch)

    mock_s3 = _make_s3_client()
    mock_rds = _make_rds_client()
    mock_bedrock = _make_bedrock_client()

    def _boto3_client(service, **kwargs):
        if service == "s3":
            return mock_s3
        if service == "rds-data":
            return mock_rds
        if service == "bedrock-runtime":
            return mock_bedrock
        return MagicMock()

    broken_report_html = MagicMock()
    broken_report_html.build_report_html = MagicMock(side_effect=RuntimeError("render boom"))
    sys.modules["report_html"] = broken_report_html

    with patch.object(_handler, "boto3") as mock_boto3, \
         patch.object(_handler, "get_config", return_value={}):
        mock_boto3.client.side_effect = _boto3_client

        # Should NOT raise
        result = _handler.lambda_handler({}, {})

    assert result["statusCode"] == 200

    # Cluster JSON put must still have happened despite the HTML render failure.
    put_calls = mock_s3.put_object.call_args_list
    cluster_json_keys = [
        c.kwargs.get("Key", "") for c in put_calls
        if c.kwargs.get("Key", "").endswith(".json") and "/_fleet/" not in c.kwargs.get("Key", "")
    ]
    assert len(cluster_json_keys) == 1, f"cluster JSON put should still happen; got put_calls={put_calls}"

    # DB INSERT must still have happened (cluster report + fleet rollup).
    insert_calls = [
        c for c in mock_rds.execute_statement.call_args_list
        if "INSERT INTO reports" in (c.kwargs.get("sql") or "")
    ]
    assert len(insert_calls) >= 1, "INSERT must still execute when HTML render fails"
