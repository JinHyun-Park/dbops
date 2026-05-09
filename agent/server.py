import os

try:
    from agent.prompts.system_prompt import build_system_prompt
except ImportError:
    from prompts.system_prompt import build_system_prompt

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel

app = BedrockAgentCoreApp()
log = app.logger

_agent_cache = {}


def get_or_create_agent(session_key: str) -> Agent:
    if session_key in _agent_cache:
        return _agent_cache[session_key]

    model = BedrockModel(
        model_id=os.environ.get("AGENT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
        region_name=os.environ.get("AWS_REGION", "ap-northeast-2"),
    )

    tools = []

    gateway_url = os.environ.get("GATEWAY_MCP_URL", "")
    if gateway_url:
        try:
            from strands.tools.mcp.mcp_client import MCPClient
            from mcp.client.streamable_http import streamablehttp_client

            client = MCPClient(lambda: streamablehttp_client(gateway_url))
            with client:
                tools.extend(client.list_tools_sync())
        except Exception as e:
            log.warning(f"Gateway MCP connection failed: {e}")

    agent = Agent(
        model=model,
        system_prompt=build_system_prompt(),
        tools=tools,
    )
    _agent_cache[session_key] = agent
    return agent


@app.entrypoint
async def invoke(payload, context):
    log.info("DBOps Agent invoked")
    session_id = getattr(context, "session_id", "default-session")
    user_id = getattr(context, "user_id", "default-user")

    agent = get_or_create_agent(f"{session_id}/{user_id}")

    prompt = payload.get("prompt") if isinstance(payload, dict) else str(payload)

    stream = agent.stream_async(prompt)
    async for event in stream:
        if isinstance(event, dict) and "data" in event and isinstance(event["data"], str):
            yield event["data"]


if __name__ == "__main__":
    app.run()
