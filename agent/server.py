import os
import sys
import time
import base64
import urllib.request
import urllib.parse
import json

_deps = os.path.join(os.path.dirname(__file__), "_deps")
if os.path.isdir(_deps) and _deps not in sys.path:
    sys.path.insert(0, _deps)

try:
    from agent.prompts.system_prompt import build_system_prompt
except ImportError:
    from prompts.system_prompt import build_system_prompt

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

app = BedrockAgentCoreApp()
log = app.logger

GATEWAY_URL = os.environ.get("GATEWAY_MCP_URL", "")
GATEWAY_TOKEN_URL = os.environ.get("GATEWAY_TOKEN_URL", "")
GATEWAY_CLIENT_ID = os.environ.get("GATEWAY_CLIENT_ID", "")
GATEWAY_CLIENT_SECRET = os.environ.get("GATEWAY_CLIENT_SECRET", "")
GATEWAY_SCOPE = os.environ.get("GATEWAY_SCOPE", "")
MODEL_ID = os.environ.get("AGENT_MODEL_ID", "apac.anthropic.claude-sonnet-4-20250514-v1:0")
REGION = os.environ.get("AWS_REGION_OVERRIDE", os.environ.get("AWS_REGION", "ap-northeast-2"))

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
    mcp_client = make_mcp_client()

    if mcp_client is None:
        agent = Agent(model=model, system_prompt=build_system_prompt(), tools=[])
        async for event in agent.stream_async(prompt):
            if isinstance(event, dict) and "data" in event and isinstance(event["data"], str):
                yield event["data"]
        return

    with mcp_client:
        try:
            tools = mcp_client.list_tools_sync()
            log.info(f"Loaded {len(tools)} tools from Gateway")
        except Exception as e:
            log.warning(f"Gateway tools load failed: {e}")
            tools = []

        agent = Agent(model=model, system_prompt=build_system_prompt(), tools=tools)
        async for event in agent.stream_async(prompt):
            if isinstance(event, dict) and "data" in event and isinstance(event["data"], str):
                yield event["data"]


if __name__ == "__main__":
    app.run()
