"""A failed schema migration must FAIL the deploy, not report success.

The migrator runs as a CDK Provider `on_event` handler. A Custom Resource that
returns without raising is a CloudFormation SUCCESS regardless of what it puts in
`Data`, so the previous `return {"Data": {"status": "failed"}}` reported a broken
migration as a completed deployment: the cache DB ends up missing tables while
every stack shows UPDATE_COMPLETE, and the first symptom is a REST route or a
collector failing much later on a table that was never created.

Idempotent re-run conflicts ("already exists", "duplicate") are classified as
`skipped` inside the handler, so `errors` only ever holds genuine failures and
raising on them does not break a normal repeat deploy.
"""

import importlib.util
import pathlib
from unittest.mock import MagicMock, patch

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SRC = _ROOT / "data-pipeline" / "schema_migrator" / "handler.py"

_ENV = {
    "CACHE_DB_CLUSTER_ARN": "arn:aws:rds:ap-northeast-2:0:cluster:cache",
    "CACHE_DB_SECRET_ARN": "arn:aws:secretsmanager:ap-northeast-2:0:secret:cache",
    "CACHE_DB_NAME": "dbops",
}


def _load():
    spec = importlib.util.spec_from_file_location("schema_migrator_handler", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(monkeypatch, tmp_path, side_effect):
    """Execute the handler against a temp sql/ dir with one schema file.

    The handler resolves sql/ as os.path.join(os.path.dirname(__file__), "sql"),
    so point listdir/open at the temp dir by patching os.path.dirname in the
    module namespace, which is the smallest seam that does not require moving the
    real asset directory."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")

    mod = _load()
    monkeypatch.setattr(mod.os.path, "dirname", lambda _p: str(tmp_path))
    monkeypatch.setattr(mod, "_run_statement", MagicMock(side_effect=side_effect))
    with patch.dict(mod.os.environ, _ENV, clear=False), \
            patch.object(mod.boto3, "client", MagicMock()):
        return mod.lambda_handler({"RequestType": "Create"}, None)


def test_genuine_error_raises_so_cloudformation_fails(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError) as exc:
        _run(monkeypatch, tmp_path, Exception('relation "metric_snapshots" does not exist'))
    # the message must name the file and carry the first error, so the
    # CloudFormation event is actionable without digging through Lambda logs
    assert "schema.sql" in str(exc.value)
    assert "metric_snapshots" in str(exc.value)


def test_idempotent_conflict_is_skipped_not_failed(tmp_path, monkeypatch):
    """A repeat deploy hits 'already exists' on every CREATE. That must stay a
    success, otherwise fail-hard would break every normal redeploy."""
    result = _run(monkeypatch, tmp_path, Exception('relation "t" already exists'))
    assert result["Data"]["status"] == "ok"


def test_clean_run_returns_ok(tmp_path, monkeypatch):
    result = _run(monkeypatch, tmp_path, None)
    assert result["Data"]["status"] == "ok"
    assert result["PhysicalResourceId"] == "dbops-schema-migrator"
