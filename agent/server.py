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

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient

app = BedrockAgentCoreApp()
log = app.logger

GATEWAY_URL = os.environ.get("GATEWAY_MCP_URL", "")
GATEWAY_TOKEN_URL = os.environ.get("GATEWAY_TOKEN_URL", "")
GATEWAY_CLIENT_ID = os.environ.get("GATEWAY_CLIENT_ID", "")
GATEWAY_CLIENT_SECRET = os.environ.get("GATEWAY_CLIENT_SECRET", "")
GATEWAY_SCOPE = os.environ.get("GATEWAY_SCOPE", "")
MODEL_ID = os.environ.get("AGENT_MODEL_ID", "apac.anthropic.claude-sonnet-4-20250514-v1:0")
REGION = os.environ.get("AWS_REGION_OVERRIDE", os.environ.get("AWS_REGION", "ap-northeast-2"))
# AWS Knowledge MCP server — AWS-hosted, public, no-auth streamable-HTTP MCP
# exposing official AWS/Aurora documentation search + read. Gives the agent
# always-current docs with zero infrastructure (no Bedrock KB / vector store).
# Empty disables it. Read-only doc lookups → connects directly, not via the
# Cedar-gated Gateway.
KNOWLEDGE_MCP_URL = os.environ.get(
    "KNOWLEDGE_MCP_URL", "https://knowledge-mcp.global.api.aws/mcp"
)

_token_cache = {"token": None, "expires_at": 0}


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


async def _call_knowledge_tool(name: str, arguments: dict) -> str:
    """One-shot call to the AWS Knowledge MCP over a FRESH connection.

    The public endpoint closes idle sessions, so holding one open across the
    model's thinking time (the persistent-MCPClient approach) left the session
    dead by the time a tool actually fired — "Connection to the MCP server was
    closed" / "client session is not running". Opening + initializing per call
    sidesteps that: each doc lookup is fully self-contained."""
    async with streamablehttp_client(KNOWLEDGE_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    parts = [getattr(b, "text", "") for b in (result.content or [])]
    text = "\n\n".join(p for p in parts if p)
    return text or "(문서 도구가 빈 응답을 반환했습니다)"


def _run_knowledge_tool(name: str, arguments: dict) -> str:
    """Run the async MCP call in a DEDICATED thread with its own event loop.

    Strands invokes tools inside its own running event loop; opening the
    mcp streamable-HTTP client's anyio task group there trips "cancel scope
    in a different task". A fresh thread + asyncio.run keeps that task group
    fully self-contained. The error reason is logged so failures aren't a
    black box."""
    import asyncio
    import threading

    box: dict = {}

    def runner():
        try:
            box["ok"] = asyncio.run(_call_knowledge_tool(name, arguments))
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=30)
    if t.is_alive():
        log.warning(f"knowledge tool {name} timed out")
        return "AWS 문서 도구 응답 시간이 초과되었습니다."
    if "err" in box:
        log.warning(f"knowledge tool {name} failed: {box['err']!r}")
        return f"AWS 문서 도구 오류: {str(box['err'])[:200]}"
    return box.get("ok", "(빈 응답)")


@tool
def search_aws_documentation(search_phrase: str) -> str:
    """Search official AWS / Amazon Aurora documentation and return ranked
    results (title + URL + context). Use for authoritative AWS behavior —
    parameter defaults, limits, error codes, version differences, upgrade
    paths. Then call read_aws_documentation on a result URL for the full text.
    Always cite the source URL in your answer."""
    return _run_knowledge_tool("aws___search_documentation", {"search_phrase": search_phrase})


@tool
def read_aws_documentation(url: str) -> str:
    """Read a single AWS documentation page by URL (use a URL returned by
    search_aws_documentation) and return its content as markdown."""
    return _run_knowledge_tool("aws___read_documentation", {"requests": [{"url": url}]})


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
        [search_aws_documentation, read_aws_documentation] if KNOWLEDGE_MCP_URL else []
    )

    tools = list(knowledge_tools)
    with contextlib.ExitStack() as stack:
        if gateway_client is not None:
            try:
                stack.enter_context(gateway_client)
                gw_tools = gateway_client.list_tools_sync()
                tools.extend(gw_tools)
                log.info(f"Loaded {len(gw_tools)} tools from Gateway")
            except Exception as e:
                log.warning(f"Gateway tools load failed: {e}")
        if knowledge_tools:
            log.info(f"Registered {len(knowledge_tools)} AWS Knowledge doc tools")

        agent = Agent(model=model, system_prompt=build_system_prompt(), tools=tools)
        async for event in agent.stream_async(prompt):
            if isinstance(event, dict) and "data" in event and isinstance(event["data"], str):
                yield event["data"]


if __name__ == "__main__":
    app.run()
