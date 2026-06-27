"""incident_embeddings backfill collector — embed un-embedded rows into pgvector."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

_C = Path(__file__).resolve().parents[3] / "data-pipeline/etl_collector/collectors/incident_embeddings.py"
_spec = importlib.util.spec_from_file_location("incident_embeddings", _C)
ie = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ie)


def _select_resp(rows, cols):
    recs = []
    for r in rows:
        rec = []
        for c in cols:
            v = r[c]
            rec.append({"longValue": v} if isinstance(v, int) else {"stringValue": v})
        recs.append(rec)
    return {"columnMetadata": [{"name": c} for c in cols], "records": recs}


def _embed_body(vec):
    body = MagicMock()
    body.read.return_value = json.dumps({"embedding": vec}).encode()
    return {"body": body}


def test_embeds_event_and_stores_vector_literal(monkeypatch):
    vec = [0.5] * ie.EMBED_DIM
    bedrock = MagicMock()
    bedrock.invoke_model.return_value = _embed_body(vec)
    monkeypatch.setattr(ie.boto3, "client", lambda *a, **k: bedrock)

    rds = MagicMock()
    rds.execute_statement.side_effect = [
        _select_resp([{"id": 1, "message": "high cpu"}], ["id", "message"]),  # event SELECT
        {},  # UPDATE event_log
        _select_resp([], ["id", "title", "summary_md", "body_md"]),  # runbook SELECT (none)
    ]

    out = ie.collect_incident_embeddings(rds, "arn", "sec", "db")

    assert out["events"] == 1 and out["runbooks"] == 0
    update = rds.execute_statement.call_args_list[1]
    assert "::vector" in update.kwargs["sql"]
    assert "UPDATE event_log" in update.kwargs["sql"]
    params = {p["name"]: p["value"] for p in update.kwargs["parameters"]}
    assert params["emb"]["stringValue"].startswith("[0.5")
    assert params["id"]["longValue"] == 1


def test_skips_store_when_embedding_fails(monkeypatch):
    bedrock = MagicMock()
    bedrock.invoke_model.side_effect = RuntimeError("bedrock down")
    monkeypatch.setattr(ie.boto3, "client", lambda *a, **k: bedrock)

    rds = MagicMock()
    rds.execute_statement.side_effect = [
        _select_resp([{"id": 1, "message": "high cpu"}], ["id", "message"]),  # event SELECT
        _select_resp([], ["id", "title", "summary_md", "body_md"]),  # runbook SELECT
    ]

    out = ie.collect_incident_embeddings(rds, "arn", "sec", "db")

    # embedding failed → no UPDATE issued, only the two SELECTs
    assert out["events"] == 0
    assert rds.execute_statement.call_count == 2
