import base64
import contextlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request

_deps = os.path.join(os.path.dirname(__file__), "_deps")
if os.path.isdir(_deps) and _deps not in sys.path:
    sys.path.insert(0, _deps)

try:
    from agent.prompts.system_prompt import build_system_prompt
except ImportError:
    from prompts.system_prompt import build_system_prompt

try:
    import agent.tenancy as tenancy
except ImportError:
    import tenancy  # type: ignore

try:
    from agent.tool_gate import ClusterVisibilityGate
except ImportError:
    from tool_gate import ClusterVisibilityGate  # type: ignore

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import get_session as _botocore_session
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

app = BedrockAgentCoreApp()
log = app.logger


def _load_context_files() -> str:
    """Concatenate operator-uploaded context files into a single string for the
    system prompt. FAIL-SAFE: any error (no env, no grant, DDB down) -> "" so the
    chat is never affected."""
    try:
        import re

        import boto3
        name = os.environ.get("CONTEXT_FILES_TABLE")
        if not name:
            return ""
        table = boto3.resource("dynamodb").Table(name)
        items, kwargs = [], {}
        while True:
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        blocks = []
        for it in items:
            if not it.get("content"):
                continue
            # Sanitize fence markers so a stored row can't break the outer fence
            safe_content = re.sub(r"OPERATOR_CONTEXT", "OPERATOR-CONTEXT", it["content"], flags=re.IGNORECASE)
            safe_name = re.sub(r"OPERATOR_CONTEXT", "OPERATOR-CONTEXT", it.get("name", ""), flags=re.IGNORECASE)
            blocks.append(f"### {safe_name}\n{safe_content}")
        return "\n\n".join(blocks)
    except Exception as e:  # noqa: BLE001 - fail-safe by design
        try:
            log.warning(f"context-files load failed: {type(e).__name__}: {e}")
        except Exception:
            pass
        return ""

GATEWAY_URL = os.environ.get("GATEWAY_MCP_URL", "")
GATEWAY_TOKEN_URL = os.environ.get("GATEWAY_TOKEN_URL", "")
GATEWAY_CLIENT_ID = os.environ.get("GATEWAY_CLIENT_ID", "")
GATEWAY_CLIENT_SECRET = os.environ.get("GATEWAY_CLIENT_SECRET", "")
GATEWAY_SCOPE = os.environ.get("GATEWAY_SCOPE", "")
MODEL_ID = os.environ.get("AGENT_MODEL_ID", "apac.anthropic.claude-sonnet-4-20250514-v1:0")
REGION = os.environ.get("AWS_REGION_OVERRIDE", os.environ.get("AWS_REGION", "ap-northeast-2"))
# AWS MCP Server — AWS-MANAGED remote MCP (SigV4-authenticated) exposing
# official AWS/Aurora documentation. We sign requests with the runtime's IAM
# role and expose ONLY the read-only doc tools — never the AWS-API-execution
# tools (call_aws/run_script) the same server also offers. Empty disables it.
# Replaces the deprecated public knowledge-mcp endpoint (whose tools/call
# returned 400 over plain streamable-HTTP).
AWS_MCP_URL = os.environ.get("AWS_MCP_URL", "https://aws-mcp.us-east-1.api.aws/mcp")
AWS_MCP_REGION = os.environ.get("AWS_MCP_REGION", "us-east-1")
AWS_MCP_SERVICE = "aws-mcp"

_token_cache = {"token": None, "expires_at": 0}


def _extract_usage(event):
    """Pull {input_tokens, output_tokens} from a Strands stream event, or None.

    The final event from stream_async is an AgentResultEvent whose dict shape is
    {"result": AgentResult}. AgentResult.metrics is an EventLoopMetrics instance
    and .accumulated_usage is a Usage TypedDict with inputTokens/outputTokens.

    Fully defensive — never raises (returns None on any unexpected shape).
    """
    try:
        result = event.get("result") if isinstance(event, dict) else None
        usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None)
        if not usage:
            return None
        inp = usage.get("inputTokens")
        out = usage.get("outputTokens")
        if inp is None and out is None:
            return None
        return {"input_tokens": int(inp or 0), "output_tokens": int(out or 0)}
    except Exception:
        return None


def get_gateway_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    if not (GATEWAY_TOKEN_URL and GATEWAY_CLIENT_ID and GATEWAY_CLIENT_SECRET):
        return None

    basic = base64.b64encode(f"{GATEWAY_CLIENT_ID}:{GATEWAY_CLIENT_SECRET}".encode()).decode()
    body = {"grant_type": "client_credentials"}
    if GATEWAY_SCOPE:
        body["scope"] = GATEWAY_SCOPE
    data = urllib.parse.urlencode(body).encode()

    req = urllib.request.Request(
        GATEWAY_TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode())

    _token_cache["token"] = payload["access_token"]
    _token_cache["expires_at"] = now + payload.get("expires_in", 3600)
    log.info(f"Gateway token issued (expires in {payload.get('expires_in')}s)")
    return _token_cache["token"]


def make_mcp_client():
    if not GATEWAY_URL:
        return None
    token = None
    try:
        token = get_gateway_token()
    except Exception as e:
        log.warning(f"Gateway token failed: {e}")
        return None
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    return MCPClient(lambda: streamablehttp_client(GATEWAY_URL, headers=headers))


def _aws_mcp_call(tool_name: str, arguments: dict, max_chars: int = 8000) -> str:
    """Call one tool on the AWS-managed MCP Server, SigV4-signed per call.

    Plain synchronous JSON-RPC over SigV4-signed POSTs (no mcp client / anyio /
    threads): initialize → notifications/initialized → tools/call, each a fresh
    signed request. Stateless, so a server-side idle timeout can never strand a
    session. Auth uses the runtime's IAM role credentials; doc reads aren't
    downstream-authorized, so no extra IAM action is required."""
    import json
    import urllib.request

    creds = _botocore_session().get_credentials()
    if creds is None:
        return "AWS 자격증명을 찾을 수 없어 문서 도구를 사용할 수 없습니다."
    frozen = creds.get_frozen_credentials()
    accept = "application/json, text/event-stream"

    def _post(body: dict, session_id=None):
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json", "Accept": accept}
        if session_id:
            headers["mcp-session-id"] = session_id
        signed = AWSRequest(method="POST", url=AWS_MCP_URL, data=data, headers=headers)
        SigV4Auth(frozen, AWS_MCP_SERVICE, AWS_MCP_REGION).add_auth(signed)
        req = urllib.request.Request(
            AWS_MCP_URL, data=data, headers=dict(signed.headers), method="POST"
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, resp.read().decode(), resp.headers.get("mcp-session-id")

    def _extract(raw: str) -> str:
        # Response is JSON-RPC; tolerate SSE framing (data: lines) just in case.
        body = raw.strip()
        if body.startswith("event:") or body.startswith("data:") or "\ndata:" in body:
            for line in body.splitlines():
                if line.startswith("data:"):
                    body = line[5:].strip()
        obj = json.loads(body)
        if "error" in obj:
            return f"AWS 문서 도구 오류: {str(obj['error'])[:200]}"
        content = (obj.get("result") or {}).get("content") or []
        parts = [c.get("text", "") for c in content if isinstance(c, dict)]
        return ("\n\n".join(p for p in parts if p))[:max_chars]

    try:
        st, _body, sid = _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "dbops-agent", "version": "1.0"}},
        })
        if st != 200 or not sid:
            log.warning(f"aws-mcp initialize failed: HTTP {st}")
            return f"AWS 문서 서버 초기화에 실패했습니다 (HTTP {st})."
        try:
            _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        except Exception:  # noqa: BLE001 - notification is best-effort
            pass
        _st, body2, _ = _post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }, sid)
        text = _extract(body2)
        return text or "(문서 도구가 빈 응답을 반환했습니다)"
    except Exception as e:  # noqa: BLE001
        log.warning(f"aws-mcp tool {tool_name} failed: {e!r}")
        return f"AWS 문서 도구 호출에 실패했습니다: {str(e)[:200]}"


@tool
def search_aws_documentation(search_phrase: str) -> str:
    """Search official AWS / Amazon Aurora documentation and return ranked
    results (title + URL + context). Use for authoritative AWS behavior —
    parameter defaults, limits, error codes, version differences, upgrade
    paths. Then call read_aws_documentation on a result URL for the full text.
    Always cite the source URL in your answer."""
    return _aws_mcp_call("aws___search_documentation", {"search_phrase": search_phrase})


@tool
def read_aws_documentation(url: str) -> str:
    """Read a single AWS documentation page by URL (use a URL returned by
    search_aws_documentation) and return its content as markdown."""
    return _aws_mcp_call(
        "aws___read_documentation", {"requests": [{"url": url, "max_length": 8000}]}
    )


def _resolve_model_id(payload) -> str:
    """Pick the Bedrock model ID per invocation.

    Accepts three shapes for `payload.model`:
      - Anthropic foundation/inference-profile ID:
          apac.anthropic.claude-..., us.anthropic.claude-..., global.anthropic.claude-...
      - Application Inference Profile ARN (per DBOps cost-tagging setup):
          arn:aws:bedrock:<region>:<account>:application-inference-profile/<id>
      - Plain foundation model ID: anthropic.claude-...

    Anything else falls back to MODEL_ID. The runtime IAM role grants
    bedrock:InvokeModel Resource:* so we don't need to extend permissions
    for newly-listed models.
    """
    if not isinstance(payload, dict):
        return MODEL_ID
    requested = payload.get("model") or payload.get("model_id") or ""
    if not requested or not isinstance(requested, str) or len(requested) > 500:
        return MODEL_ID

    lower = requested.lower()
    # Anthropic foundation/inference profile IDs.
    if "anthropic" in lower:
        return requested
    # DBOps Application Inference Profile ARNs (the cost-tagged path).
    if lower.startswith("arn:aws:bedrock:") and "application-inference-profile/" in lower:
        return requested
    return MODEL_ID


@app.entrypoint
async def invoke(payload, context):
    log.info(f"DBOps Agent invoked. Gateway: {bool(GATEWAY_URL)}")
    prompt = payload.get("prompt") if isinstance(payload, dict) else str(payload)
    requested_model = _resolve_model_id(payload)

    headers = getattr(context, "request_headers", None) or {}
    try:
        visible = tenancy.visible_cluster_ids_for(headers)
    except Exception as e:
        log.warning(f"tenancy resolve failed: {type(e).__name__}")
        visible = None  # fail-open -- never break chat
    try:
        _dh = {k: ("<redacted>" if k.lower() == "authorization" else v) for k, v in (headers or {}).items()}
        _dc = tenancy._claims_from_headers(headers)
        _du = (_dc.get("cognito:username") or _dc.get("sub") or "")
        _dt = set() if tenancy._is_admin(_dc) else tenancy._my_team_ids(_du)
        log.info(
            "[tenancy-diag] header_keys=%s claims=%s user=...%s admin=%s groups=%s teams=%s visible=%s",
            list(_dh.keys()), bool(_dc), _du[-6:], tenancy._is_admin(_dc),
            _dc.get("cognito:groups"), sorted(_dt),
            ("None(all)" if visible is None else len(visible)),
        )
    except Exception as e:
        log.warning(f"[tenancy-diag] failed: {type(e).__name__}: {e}")

    # First attempt: requested model. If Bedrock rejects with ValidationException,
    # we silently fall back to the env default so a stale frontend never bricks chat.
    model_id = requested_model
    log.info(f"Model: {model_id}")
    try:
        model = BedrockModel(model_id=model_id, region_name=REGION)
        # cheap dry-call avoided; BedrockModel itself doesn't validate. We rely on
        # the stream invocation to raise; the wrapper around stream_async below
        # will rebuild with MODEL_ID if it sees ValidationException once.
    except Exception as e:
        log.warning(f"Model init failed for {model_id}: {e}; falling back to {MODEL_ID}")
        model_id = MODEL_ID
        model = BedrockModel(model_id=model_id, region_name=REGION)
    gateway_client = make_mcp_client()

    # AWS Knowledge doc tools are STATELESS local tools (fresh connection per
    # call — see _call_knowledge_tool), so they need no persistent context and
    # just get appended. The Gateway client DOES need its context held open
    # for the duration of streaming (AWS keeps that session alive), so it
    # stays inside the ExitStack.
    knowledge_tools = (
        [search_aws_documentation, read_aws_documentation] if AWS_MCP_URL else []
    )

    tools = list(knowledge_tools)
    with contextlib.ExitStack() as stack:
        if gateway_client is not None:
            try:
                stack.enter_context(gateway_client)
                # The Gateway paginates tools/list and list_tools_sync returns a
                # SINGLE page (~30 tools). Follow the cursor so every target's
                # tools load — otherwise whatever spills onto later pages is
                # silently missing from the agent (we had 36 defined, 30 loaded).
                gw_tools = []
                page = gateway_client.list_tools_sync()
                gw_tools.extend(page)
                guard = 0
                while getattr(page, "pagination_token", None) and guard < 20:
                    page = gateway_client.list_tools_sync(
                        pagination_token=page.pagination_token
                    )
                    gw_tools.extend(page)
                    guard += 1
                tools.extend(gw_tools)
                log.info(f"Loaded {len(gw_tools)} tools from Gateway")
            except Exception as e:
                log.warning(f"Gateway tools load failed: {e}")
        if knowledge_tools:
            log.info(f"Registered {len(knowledge_tools)} AWS Knowledge doc tools")

        agent = Agent(
            model=model,
            system_prompt=build_system_prompt(_load_context_files(), visible_clusters=visible),
            tools=tools,
            hooks=[ClusterVisibilityGate(visible)],
        )
        last_usage = None
        async for event in agent.stream_async(prompt):
            if isinstance(event, dict) and "data" in event and isinstance(event["data"], str):
                yield event["data"]
            try:
                u = _extract_usage(event)
                if u:
                    last_usage = u
            except Exception:
                pass
        if last_usage:
            try:
                yield json.dumps({"type": "usage", **last_usage})
            except Exception:
                pass


if __name__ == "__main__":
    app.run()
