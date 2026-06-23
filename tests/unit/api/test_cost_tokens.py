import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

_H = Path(__file__).resolve().parents[3] / "api" / "cost" / "handler.py"
_s = importlib.util.spec_from_file_location("cost_handler", _H)
handler = importlib.util.module_from_spec(_s)
_s.loader.exec_module(handler)


def _event(view="tokens", days="30"):
    return {"requestContext": {"http": {"method": "GET"}},
            "queryStringParameters": {"view": view, "days": days}}


def _fake_cw():
    cw = MagicMock()
    cw.list_metrics.return_value = {
        "Metrics": [
            {"Dimensions": [{"Name": "ModelId", "Value": "anthropic.claude-sonnet-4-6"}]},
            {"Dimensions": [{"Name": "ModelId", "Value": "anthropic.claude-opus-4-8"}]},
        ]
    }
    # get_metric_data returns one result per (model, input|output) query id.
    def _gmd(MetricDataQueries, **kw):
        results = []
        for q in MetricDataQueries:
            results.append({"Id": q["Id"], "Timestamps": [], "Values": [100.0]})
        return {"MetricDataResults": results}
    cw.get_metric_data.side_effect = _gmd
    return cw


def test_tokens_view_aggregates_by_model():
    with patch.object(handler.boto3, "client") as mk:
        mk.return_value = _fake_cw()
        r = handler.lambda_handler(_event())
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["view"] == "tokens"
    models = {m["model"] for m in body["by_model"]}
    assert "anthropic.claude-sonnet-4-6" in models
    assert all("input" in m and "output" in m and "total" in m for m in body["by_model"])
    assert "note" in body


def test_tokens_view_empty_metrics_is_valid():
    cw = MagicMock()
    cw.list_metrics.return_value = {"Metrics": []}
    cw.get_metric_data.return_value = {"MetricDataResults": []}
    with patch.object(handler.boto3, "client") as mk:
        mk.return_value = cw
        r = handler.lambda_handler(_event())
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["by_model"] == []


def test_default_view_does_not_call_cloudwatch():
    # ?view=bedrock (default) must not touch the tokens path / cloudwatch client.
    with patch.object(handler, "_handle_tokens_view") as th:
        # ce path will try real CE; we only assert the tokens handler isn't invoked.
        try:
            handler.lambda_handler(_event(view="bedrock"))
        except Exception:
            pass
        th.assert_not_called()
