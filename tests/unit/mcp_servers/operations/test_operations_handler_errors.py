"""The operations handler catch-all must not leak exception text into a tool
response (project hard rule: static reason + logger).

It used to return {"error": str(e)} for EVERY operations tool, so any unhandled
boto3/psycopg error put ARNs, secret names, host names or SQL fragments straight
into the agent transcript the DBA reads.
"""

import json
import logging
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("CACHE_DB_CLUSTER_ARN", "arn:aws:rds:ap-northeast-2:0:cluster:test")
os.environ.setdefault("CACHE_DB_SECRET_ARN", "arn:aws:secretsmanager:ap-northeast-2:0:secret:test")
os.environ.setdefault("CACHE_DB_NAME", "dbops")

import mcp_servers.operations.handler as handler  # noqa: E402

_SECRET = "arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:db-master-AbCdEf"


class _Ctx:
    def __init__(self, tool_name):
        self.client_context = MagicMock()
        self.client_context.custom = {"bedrockAgentCoreToolName": f"x___{tool_name}"}


def _invoke(tool_name, event):
    return json.loads(handler.lambda_handler(event, _Ctx(tool_name))["content"][0]["text"])


def test_tool_exception_is_masked_in_the_response(caplog):
    def boom(cache, **kw):
        raise RuntimeError(f"AccessDeniedException: cannot read {_SECRET}")

    with caplog.at_level(logging.ERROR, logger=handler.logger.name), patch.dict(
        handler.TOOLS["get_runbook"], {"impl": boom}
    ):
        result = _invoke("get_runbook", {"cluster_id": "c1"})

    blob = " ".join(str(v) for v in result.values())
    for leak in (_SECRET, "AccessDeniedException", "RuntimeError", "Traceback", "123456789012"):
        assert leak not in blob, f"raw exception text leaked into response: {result}"
    assert result["status"] == "error"
    # ...and the detail is still recoverable by the operator, in the Lambda log.
    assert _SECRET in caplog.text
