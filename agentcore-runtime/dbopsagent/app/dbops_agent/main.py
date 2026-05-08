from typing import Any

from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model
from mcp_client.client import get_streamable_http_mcp_client
from memory.session import get_memory_session_manager

app = BedrockAgentCoreApp()
log = app.logger

# Define a Streamable HTTP MCP Client
mcp_clients = [get_streamable_http_mcp_client()]

SYSTEM_PROMPT = """당신은 DBA를 위한 AI 데이터베이스 운영 전문가입니다.
Amazon Aurora MySQL/PostgreSQL 클러스터의 성능 분석, 장애 진단, 운영 자동화를 돕습니다.

## 규칙
1. 모든 분석은 데이터에 기반합니다. 추측하지 마세요.
2. 도구를 호출하여 실제 데이터를 확인한 후 답변하세요.
3. 변경 작업(DDL, DML, 파라미터 변경)은 반드시 사용자 승인이 필요합니다.
4. 위험한 작업은 영향 분석과 롤백 계획을 먼저 제시하세요.
5. 한국어로 답변하세요.

## Aurora 핵심 파라미터
### PostgreSQL
- shared_buffers: 인스턴스 메모리의 25-40%
- work_mem: 정렬/해시 메모리. 기본 4MB, 복잡 쿼리 시 16-64MB
- max_connections: db.r6g.large=1600, db.r6g.xlarge=3200

### MySQL
- innodb_buffer_pool_size: Aurora 자동 관리. 75% of RAM
- max_connections: GREATEST({DBInstanceClassMemory/9531392}, 5000)
- long_query_time: 슬로우 쿼리 기준. 권장 1-2초

## 진단 워크플로
1. 성능 저하 → PI 메트릭(AAS) → Top Wait Events
2. IO Wait → 인덱스 확인 → EXPLAIN → 인덱스 추천
3. Lock Wait → pg_locks/innodb_lock_waits → Blocking 쿼리
4. CPU Wait → Top SQL → 쿼리 최적화
"""

tools = []
for mcp_client in mcp_clients:
    if mcp_client:
        tools.append(mcp_client)


def agent_factory():
    cache = {}
    def get_or_create_agent(session_id, user_id):
        key = f"{session_id}/{user_id}"
        if key not in cache:
            # Create an agent for the given session_id and user_id
            cache[key] = Agent(
                model=load_model(),
                session_manager=get_memory_session_manager(session_id, user_id),
                system_prompt=SYSTEM_PROMPT,
                tools=tools
            )
        return cache[key]
    return get_or_create_agent
get_or_create_agent = agent_factory()


@app.entrypoint
async def invoke(payload, context):
    log.info("Invoking Agent.....")

    session_id = getattr(context, 'session_id', 'default-session')
    user_id = getattr(context, 'user_id', 'default-user')
    agent = get_or_create_agent(session_id, user_id)

    # Execute and format response
    stream = agent.stream_async(payload.get("prompt"))

    async for event in stream:
        # Handle Text parts of the response
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]


if __name__ == "__main__":
    app.run()
