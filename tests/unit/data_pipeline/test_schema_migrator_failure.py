"""A failed schema migration must FAIL the deploy, not report success.

The migrator runs as a CDK Provider `on_event` handler. A Custom Resource that
returns without raising is a CloudFormation SUCCESS regardless of what it puts in
`Data`, so the previous `return {"Data": {"status": "failed"}}` reported a broken
migration as a completed deployment: the cache DB ends up missing tables while
every stack shows UPDATE_COMPLETE, and the first symptom is a REST route or a
collector failing much later on a table that was never created.

Three things must stay true about that raise:

* it must NOT fire on RequestType=Delete (nothing to migrate on a destroy, and
  raising there parks the stack in DELETE_FAILED),
* it must NOT fire for transient Data API errors (a cache writer resuming from
  its 0.5-ACU floor, or request throttling). Those are retried, and if they
  survive the retries they raise a separate, explicitly-retryable message,
* it must NOT quote exception text: the failure Reason surfaces in stack events
  and `cdk deploy` output, and Data API errors can carry the secret ARN.

Idempotent re-run conflicts ("already exists", "duplicate") are classified as
`skipped` inside the handler, so raising does not break a normal repeat deploy.
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

_RESUMING = (
    "DatabaseResumingException: The DB cluster is resuming after being auto-paused. "
    f"resourceArn={_ENV['CACHE_DB_CLUSTER_ARN']} secretArn={_ENV['CACHE_DB_SECRET_ARN']}"
)


def _load():
    spec = importlib.util.spec_from_file_location("schema_migrator_handler", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(monkeypatch, tmp_path, side_effect, request_type="Create"):
    """Execute the handler against a temp sql/ dir with one schema file.

    The handler resolves sql/ as os.path.join(os.path.dirname(__file__), "sql"),
    so point listdir/open at the temp dir by patching os.path.dirname in the
    module namespace, which is the smallest seam that does not require moving the
    real asset directory. Returns (result_or_None, run_statement_mock)."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")

    mod = _load()
    runner = MagicMock(side_effect=side_effect)
    monkeypatch.setattr(mod.os.path, "dirname", lambda _p: str(tmp_path))
    monkeypatch.setattr(mod, "_run_statement", runner)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)  # no real backoff in tests
    with patch.dict(mod.os.environ, _ENV, clear=False), \
            patch.object(mod.boto3, "client", MagicMock()):
        return mod.lambda_handler({"RequestType": request_type}, None), runner


def test_genuine_error_raises_so_cloudformation_fails(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError) as exc:
        _run(monkeypatch, tmp_path, Exception('relation "metric_snapshots" does not exist'))
    msg = str(exc.value)
    # the message must name the failing file and point at the log group, so the
    # CloudFormation event is actionable...
    assert "schema.sql" in msg
    assert "SchemaMigrator" in msg
    # ...but must NOT quote the driver error itself (see leak test below)
    assert "metric_snapshots" not in msg


def test_failure_reason_leaks_no_arn_or_exception_text(tmp_path, monkeypatch):
    """The Reason is echoed in stack events / cdk output, a wider audience than the
    log group. Data API errors quote resourceArn/secretArn, so nothing from the
    exception may reach the raise."""
    leaky = Exception(
        "BadRequestException: syntax error at or near \"CREAT\"; "
        f"secretArn={_ENV['CACHE_DB_SECRET_ARN']}"
    )
    with pytest.raises(RuntimeError) as exc:
        _run(monkeypatch, tmp_path, leaky)
    msg = str(exc.value)
    assert "arn:" not in msg
    assert "secretArn" not in msg
    assert "syntax error" not in msg
    assert "BadRequestException" not in msg
    assert "schema.sql" in msg


def test_delete_is_a_noop_and_never_raises(tmp_path, monkeypatch):
    """cdk destroy sends RequestType=Delete to the same on_event handler. There is
    nothing to migrate, and raising would leave the stack in DELETE_FAILED."""
    result, runner = _run(
        monkeypatch, tmp_path, Exception(_RESUMING), request_type="Delete"
    )
    assert result["PhysicalResourceId"] == "dbops-schema-migrator"
    assert runner.call_count == 0  # no schema file was processed at all


def test_transient_error_is_retried_then_succeeds(tmp_path, monkeypatch):
    """A cold 0.5-ACU writer fails the first statement with DatabaseResumingException
    and accepts the retry. That must be a successful deploy."""
    result, runner = _run(monkeypatch, tmp_path, [Exception(_RESUMING), None])
    assert result["Data"]["status"] == "ok"
    assert runner.call_count == 2


def test_persistent_transient_is_not_reported_as_a_ddl_failure(tmp_path, monkeypatch):
    """If the DB never answers, the deploy still fails (tables are missing), but the
    message must say retryable-infrastructure, not broken-schema, and still leak
    nothing."""
    with pytest.raises(RuntimeError) as exc:
        _run(monkeypatch, tmp_path, Exception("ThrottlingException: Rate exceeded"))
    msg = str(exc.value)
    assert "transient" in msg
    assert "re-run" in msg
    assert "schema migration failed" not in msg  # distinct from the genuine-DDL raise
    assert "ThrottlingException" not in msg
    assert "arn:" not in msg


def test_idempotent_conflict_is_skipped_not_failed(tmp_path, monkeypatch):
    """A repeat deploy hits 'already exists' on every CREATE. That must stay a
    success, otherwise fail-hard would break every normal redeploy."""
    result, _ = _run(monkeypatch, tmp_path, Exception('relation "t" already exists'))
    assert result["Data"]["status"] == "ok"


def test_clean_run_returns_ok(tmp_path, monkeypatch):
    result, _ = _run(monkeypatch, tmp_path, None)
    assert result["Data"]["status"] == "ok"
    assert result["PhysicalResourceId"] == "dbops-schema-migrator"
