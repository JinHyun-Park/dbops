import os

try:
    from agent.prompts.system_prompt import build_system_prompt
except ImportError:
    from prompts.system_prompt import build_system_prompt


def create_agent():
    from strands import Agent
    from strands.models import BedrockModel
    from strands.tools.mcp.mcp_client import MCPClient
    from mcp.client.streamable_http import streamablehttp_client

    model = BedrockModel(
        model_id=os.environ.get("AGENT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
        region_name=os.environ.get("AWS_REGION", "ap-northeast-2"),
    )

    gateway_id = os.environ.get("GATEWAY_ID", "")
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    gateway_url = f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"

    tools = []
    try:
        from strands_tools import retrieve
        tools.append(retrieve)
    except ImportError:
        pass

    gateway_client = MCPClient(lambda: streamablehttp_client(gateway_url))

    with gateway_client:
        gateway_tools = gateway_client.list_tools_sync()
        tools.extend(gateway_tools)

        agent = Agent(
            model=model,
            system_prompt=build_system_prompt(),
            tools=tools,
        )

    return agent


if __name__ == "__main__":
    agent = create_agent()
    print("DBOps Agent ready.")
