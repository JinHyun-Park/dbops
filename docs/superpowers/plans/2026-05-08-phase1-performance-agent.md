# Phase 1: Performance Analysis Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** DBA가 자연어 대화로 Aurora MySQL/PostgreSQL 클러스터의 성능을 분석할 수 있는 MVP 구축

**Architecture:** Single Strands Agent on AgentCore Runtime, connected to Performance MCP Server (4 custom tools) + official AWS MCP servers via AgentCore Gateway. Data collected by ETL Lambda into Aurora PG Cache. Next.js frontend with SSE direct for chat + REST API for dashboard. CDK 4-stack deployment.

**Tech Stack:** Python 3.10+, Strands Agents SDK, AWS CDK (Python), AgentCore Runtime/Gateway, Next.js 15, React, TypeScript, shadcn/ui, Tailwind CSS, Aurora PostgreSQL Serverless v2, DynamoDB, S3, Bedrock KB + S3 Vectors

**Spec:** `docs/superpowers/specs/2026-05-08-dbops-design.md`
**Kiro Spec:** `.kiro/specs/phase1-performance-agent/`

---

## File Structure (Phase 1)

```
dbops/
├── cdk/
│   ├── app.py
│   ├── requirements.txt
│   ├── cdk.json
│   ├── config/
│   │   ├── settings.py
│   │   └── settings.example.py
│   └── stacks/
│       ├── __init__.py
│       ├── foundation_stack.py
│       ├── data_stack.py
│       ├── agent_stack.py
│       └── frontend_stack.py
├── agent/
│   ├── server.py
│   ├── prompts/
│   │   ├── __init__.py
│   │   ├── system_prompt.py
│   │   └── cheatsheet.py
│   ├── Dockerfile
│   └── requirements.txt
├── mcp-servers/
│   ├── shared/
│   │   ├── __init__.py
│   │   ├── cache_client.py
│   │   └── models.py
│   └── performance/
│       ├── handler.py
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── top_queries.py
│       │   ├── pi_metrics.py
│       │   ├── slow_queries.py
│       │   └── compare_periods.py
│       └── requirements.txt
├── data-pipeline/
│   ├── etl_collector/
│   │   ├── handler.py
│   │   ├── collectors/
│   │   │   ├── __init__.py
│   │   │   ├── pi_collector.py
│   │   │   ├── stats_collector.py
│   │   │   └── meta_collector.py
│   │   └── requirements.txt
│   └── sql/
│       └── schema.sql
├── api/
│   ├── dashboard/
│   │   └── handler.py
│   └── clusters/
│       └── handler.py
├── frontend/
│   ├── package.json
│   ├── next.config.ts
│   ├── tailwind.config.ts
│   ├── tsconfig.json
│   ├── src/
│   │   ├── app/
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx
│   │   │   ├── chat/
│   │   │   │   └── page.tsx
│   │   │   └── dashboard/
│   │   │       └── page.tsx
│   │   ├── components/
│   │   │   ├── design-system/
│   │   │   │   ├── metric-card.tsx
│   │   │   │   └── status-badge.tsx
│   │   │   ├── chat/
│   │   │   │   ├── chat-panel.tsx
│   │   │   │   ├── message-list.tsx
│   │   │   │   └── tool-status.tsx
│   │   │   └── dashboard/
│   │   │       ├── cluster-overview.tsx
│   │   │       └── aas-chart.tsx
│   │   ├── lib/
│   │   │   ├── agentcore-sse.ts
│   │   │   ├── api-client.ts
│   │   │   └── auth.ts
│   │   └── styles/
│   │       └── globals.css
│   └── public/
├── knowledge/
│   └── aurora-docs/
│       └── README.md
├── tests/
│   ├── unit/
│   │   ├── mcp_servers/
│   │   │   └── performance/
│   │   │       ├── test_top_queries.py
│   │   │       ├── test_pi_metrics.py
│   │   │       ├── test_slow_queries.py
│   │   │       └── test_compare_periods.py
│   │   └── data_pipeline/
│   │       └── test_etl_collector.py
│   └── conftest.py
├── requirements.txt
├── pyproject.toml
├── CLAUDE.md
├── AGENTS.md
└── .gitignore
```

---

## Task 1: Project Initialization

**Files:**
- Create: `requirements.txt`, `pyproject.toml`, `.gitignore`, `CLAUDE.md`
- Create: `cdk/requirements.txt`, `cdk/cdk.json`, `cdk/app.py`
- Create: `cdk/config/settings.example.py`, `cdk/config/settings.py`

- [ ] **Step 1: Create root Python project files**

```python
# pyproject.toml
[project]
name = "dbops"
version = "0.1.0"
requires-python = ">=3.10"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

```txt
# requirements.txt
strands-agents>=1.0.0
strands-agents-tools>=0.1.0
boto3>=1.35.0
psycopg2-binary>=2.9.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
moto[all]>=5.0.0
```

- [ ] **Step 2: Create .gitignore**

```gitignore
# .gitignore
__pycache__/
*.py[cod]
*.egg-info/
dist/
.venv/
venv/
node_modules/
.next/
out/
cdk.out/
.env
cdk/config/settings.py
*.js.map
```

- [ ] **Step 3: Create CLAUDE.md**

```markdown
# CLAUDE.md
# DBOps — AI-Powered Database Operations Platform

## Quick Reference
- Spec: docs/superpowers/specs/2026-05-08-dbops-design.md
- Plans: docs/superpowers/plans/
- Kiro specs: .kiro/specs/

## Commands
- CDK deploy: `cd cdk && cdk deploy --all`
- Tests: `pytest tests/ -v`
- Frontend dev: `cd frontend && npm run dev`
- Frontend build: `cd frontend && npm run build`

## Rules
- All infrastructure changes via CDK only. Never use AWS CLI to modify resources.
- All agent SQL queries must include `/* source=dbops-agent */` comment.
- DB write operations require human approval (Cedar Policy enforced).
- Environment config in `cdk/config/settings.py` (gitignored).
```

- [ ] **Step 4: Create CDK project files**

```json
// cdk/cdk.json
{
  "app": "python3 app.py",
  "context": {
    "@aws-cdk/core:stackRelativeExports": true
  }
}
```

```txt
# cdk/requirements.txt
aws-cdk-lib>=2.170.0
constructs>=10.0.0
```

```python
# cdk/config/settings.example.py
class Settings:
    ENV = "dev"
    REGION = "ap-northeast-2"
    ACCOUNT_ID = "123456789012"

    COGNITO_DOMAIN_PREFIX = "dbops-dev"
    CALLBACK_URLS = ["http://localhost:3000/callback"]

    AGENT_MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"
    GATEWAY_SEMANTIC_SEARCH = True

    PI_COLLECTION_INTERVAL_MIN = 1
    STATS_COLLECTION_INTERVAL_MIN = 5

    CACHE_DB_MIN_ACU = 0.5
    CACHE_DB_MAX_ACU = 4
```

```python
# cdk/config/settings.py (local copy, gitignored)
# Copy from settings.example.py and edit for your environment
from config.settings_example import Settings  # noqa: F401
```

Wait — `settings.py` is gitignored so we need a fallback. Better approach:

```python
# cdk/config/settings.py
# Copy settings.example.py to this file and edit for your environment.
# This file is gitignored.

class Settings:
    ENV = "dev"
    REGION = "ap-northeast-2"
    ACCOUNT_ID = "123456789012"

    COGNITO_DOMAIN_PREFIX = "dbops-dev"
    CALLBACK_URLS = ["http://localhost:3000/callback"]

    AGENT_MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"
    GATEWAY_SEMANTIC_SEARCH = True

    PI_COLLECTION_INTERVAL_MIN = 1
    STATS_COLLECTION_INTERVAL_MIN = 5

    CACHE_DB_MIN_ACU = 0.5
    CACHE_DB_MAX_ACU = 4
```

- [ ] **Step 5: Create CDK app entry point**

```python
# cdk/app.py
import aws_cdk as cdk
from config.settings import Settings
from stacks.foundation_stack import FoundationStack
from stacks.data_stack import DataStack
from stacks.agent_stack import AgentStack
from stacks.frontend_stack import FrontendStack

app = cdk.App()

env = cdk.Environment(account=Settings.ACCOUNT_ID, region=Settings.REGION)

foundation = FoundationStack(app, f"dbops-{Settings.ENV}-foundation", env=env)
data = DataStack(app, f"dbops-{Settings.ENV}-data", env=env, foundation=foundation)
agent = AgentStack(app, f"dbops-{Settings.ENV}-agent", env=env, foundation=foundation, data=data)
FrontendStack(app, f"dbops-{Settings.ENV}-frontend", env=env, foundation=foundation, agent=agent)

app.synth()
```

```python
# cdk/stacks/__init__.py
```

- [ ] **Step 6: Commit**

```bash
git add .gitignore requirements.txt pyproject.toml CLAUDE.md cdk/
git commit -m "chore: initialize project structure with CDK app"
```

---

## Task 2: Foundation CDK Stack

**Files:**
- Create: `cdk/stacks/foundation_stack.py`
- Test: CDK synth validation

- [ ] **Step 1: Write Foundation Stack**

```python
# cdk/stacks/foundation_stack.py
import aws_cdk as cdk
from aws_cdk import (
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
)
from constructs import Construct
from config.settings import Settings


class FoundationStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name=f"dbops-{Settings.ENV}",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.user_pool_client = self.user_pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.PROFILE],
                callback_urls=Settings.CALLBACK_URLS,
            ),
            generate_secret=False,
        )

        self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=Settings.COGNITO_DOMAIN_PREFIX,
            ),
        )

        self.clusters_table = dynamodb.Table(
            self, "ClustersTable",
            table_name=f"dbops-{Settings.ENV}-clusters",
            partition_key=dynamodb.Attribute(name="cluster_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.sessions_table = dynamodb.Table(
            self, "SessionsTable",
            table_name=f"dbops-{Settings.ENV}-sessions",
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.hub_role = iam.Role(
            self, "HubRole",
            role_name=f"dbops-{Settings.ENV}-hub-role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole"),
            ],
        )
        self.hub_role.add_to_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole"],
            resources=["arn:aws:iam::*:role/dbops-spoke-role"],
        ))

        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)
```

- [ ] **Step 2: Verify CDK synth**

Run: `cd cdk && pip install -r requirements.txt && cdk synth dbops-dev-foundation --quiet`
Expected: CloudFormation template generated without errors.

- [ ] **Step 3: Commit**

```bash
git add cdk/stacks/foundation_stack.py
git commit -m "feat(cdk): add Foundation stack with Cognito, DynamoDB, IAM hub role"
```

---

## Task 3: Data CDK Stack + Schema

**Files:**
- Create: `cdk/stacks/data_stack.py`
- Create: `data-pipeline/sql/schema.sql`

- [ ] **Step 1: Write Aurora PG Cache schema**

```sql
-- data-pipeline/sql/schema.sql
CREATE TABLE IF NOT EXISTS cluster_meta (
    cluster_id VARCHAR(255) PRIMARY KEY,
    account_id VARCHAR(12) NOT NULL,
    region VARCHAR(20) NOT NULL,
    engine VARCHAR(20) NOT NULL,
    engine_version VARCHAR(20),
    instance_class VARCHAR(50),
    status VARCHAR(20),
    endpoint TEXT,
    reader_endpoint TEXT,
    storage_size_gb DECIMAL(10,2),
    max_connections INT,
    spoke_role_arn TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    id BIGSERIAL,
    cluster_id VARCHAR(255) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    metric_type VARCHAR(50) NOT NULL,
    value DOUBLE PRECISION,
    dimensions JSONB,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE TABLE IF NOT EXISTS query_stats (
    id BIGSERIAL,
    cluster_id VARCHAR(255) NOT NULL,
    snapshot_time TIMESTAMPTZ NOT NULL,
    query_hash VARCHAR(64) NOT NULL,
    query_text TEXT,
    calls BIGINT,
    total_time_ms DOUBLE PRECISION,
    mean_time_ms DOUBLE PRECISION,
    rows_returned BIGINT,
    shared_blks_hit BIGINT,
    shared_blks_read BIGINT,
    PRIMARY KEY (id, snapshot_time)
) PARTITION BY RANGE (snapshot_time);

CREATE TABLE IF NOT EXISTS slow_queries (
    id BIGSERIAL,
    cluster_id VARCHAR(255) NOT NULL,
    ts TIMESTAMPTZ NOT NULL,
    query_text TEXT,
    execution_time_ms DOUBLE PRECISION,
    lock_time_ms DOUBLE PRECISION,
    rows_examined BIGINT,
    rows_sent BIGINT,
    db_name VARCHAR(255),
    user_name VARCHAR(255),
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX idx_metric_snapshots_lookup ON metric_snapshots (cluster_id, metric_type, ts);
CREATE INDEX idx_query_stats_lookup ON query_stats (cluster_id, snapshot_time);
CREATE INDEX idx_slow_queries_lookup ON slow_queries (cluster_id, ts);
```

- [ ] **Step 2: Write Data Stack**

```python
# cdk/stacks/data_stack.py
import aws_cdk as cdk
from aws_cdk import (
    aws_rds as rds,
    aws_ec2 as ec2,
    aws_s3 as s3,
    aws_events as events,
    aws_events_targets as targets,
    aws_lambda as lambda_,
    aws_lambda_python_alpha as lambda_python,
)
from constructs import Construct
from config.settings import Settings


class DataStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=1,
        )

        self.cache_db = rds.DatabaseCluster(
            self, "CacheDB",
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.VER_15_10,
            ),
            serverless_v2_min_capacity=Settings.CACHE_DB_MIN_ACU,
            serverless_v2_max_capacity=Settings.CACHE_DB_MAX_ACU,
            writer=rds.ClusterInstance.serverless_v2("writer"),
            vpc=self.vpc,
            default_database_name="dbops",
            enable_data_api=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.archive_bucket = s3.Bucket(
            self, "ArchiveBucket",
            bucket_name=f"dbops-{Settings.ENV}-archive-{Settings.ACCOUNT_ID}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        self.etl_lambda = lambda_python.PythonFunction(
            self, "ETLCollector",
            entry="data-pipeline/etl_collector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            timeout=cdk.Duration.minutes(5),
            memory_size=512,
            vpc=self.vpc,
            environment={
                "CACHE_DB_CLUSTER_ARN": self.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": self.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "HUB_ROLE_ARN": foundation.hub_role.role_arn,
            },
        )

        self.cache_db.secret.grant_read(self.etl_lambda)
        self.cache_db.grant_data_api_access(self.etl_lambda)
        foundation.clusters_table.grant_read_data(self.etl_lambda)
        foundation.hub_role.grant(self.etl_lambda.role, "sts:AssumeRole")

        events.Rule(
            self, "ETLSchedule",
            schedule=events.Schedule.rate(
                cdk.Duration.minutes(Settings.STATS_COLLECTION_INTERVAL_MIN)
            ),
            targets=[targets.LambdaFunction(self.etl_lambda)],
        )

        cdk.CfnOutput(self, "CacheDbClusterArn", value=self.cache_db.cluster_arn)
        cdk.CfnOutput(self, "CacheDbEndpoint", value=self.cache_db.cluster_endpoint.hostname)
```

- [ ] **Step 3: Verify CDK synth**

Run: `cd cdk && cdk synth dbops-dev-data --quiet`
Expected: Template generated. May warn about missing Lambda code directory — that's expected, we'll create it in the next task.

- [ ] **Step 4: Commit**

```bash
git add cdk/stacks/data_stack.py data-pipeline/sql/schema.sql
git commit -m "feat(cdk): add Data stack with Aurora PG Cache, S3, ETL Lambda"
```

---

## Task 4: Shared MCP Utilities

**Files:**
- Create: `mcp-servers/shared/__init__.py`
- Create: `mcp-servers/shared/cache_client.py`
- Create: `mcp-servers/shared/models.py`
- Test: `tests/unit/mcp_servers/test_cache_client.py`

- [ ] **Step 1: Write test for cache client**

```python
# tests/conftest.py
import pytest

@pytest.fixture
def sample_cluster_id():
    return "prod-aurora-pg-1"
```

```python
# tests/unit/mcp_servers/__init__.py
```

```python
# tests/unit/mcp_servers/performance/__init__.py
```

```python
# tests/unit/mcp_servers/test_cache_client.py
from mcp_servers.shared.cache_client import CacheClient


def test_build_query_with_cluster_filter():
    client = CacheClient.__new__(CacheClient)
    sql, params = client._build_query(
        table="query_stats",
        cluster_id="prod-pg-1",
        time_column="snapshot_time",
        start_time="2026-05-01T00:00:00Z",
        end_time="2026-05-02T00:00:00Z",
        order_by="total_time_ms DESC",
        limit=10,
    )
    assert "query_stats" in sql
    assert "cluster_id" in sql
    assert "snapshot_time >=" in sql
    assert "ORDER BY total_time_ms DESC" in sql
    assert "LIMIT 10" in sql
    assert params["cluster_id"] == "prod-pg-1"


def test_build_query_without_time_range():
    client = CacheClient.__new__(CacheClient)
    sql, params = client._build_query(
        table="cluster_meta",
        cluster_id="prod-pg-1",
    )
    assert "cluster_meta" in sql
    assert "snapshot_time" not in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp_servers/test_cache_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp_servers'`

- [ ] **Step 3: Implement cache client**

```python
# mcp-servers/shared/__init__.py
```

```python
# mcp-servers/shared/models.py
from dataclasses import dataclass
from typing import Optional


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    row_count: int


@dataclass
class MetricPoint:
    timestamp: str
    value: float
    dimensions: Optional[dict] = None
```

```python
# mcp-servers/shared/cache_client.py
import os
import boto3
from typing import Optional
from mcp_servers.shared.models import QueryResult


class CacheClient:
    def __init__(self):
        self.rds_data = boto3.client("rds-data")
        self.cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
        self.secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
        self.database = os.environ.get("CACHE_DB_NAME", "dbops")

    def _build_query(
        self,
        table: str,
        cluster_id: str,
        time_column: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
        extra_where: Optional[str] = None,
    ) -> tuple[str, dict]:
        conditions = ["cluster_id = :cluster_id"]
        params = {"cluster_id": cluster_id}

        if time_column and start_time:
            conditions.append(f"{time_column} >= :start_time")
            params["start_time"] = start_time
        if time_column and end_time:
            conditions.append(f"{time_column} < :end_time")
            params["end_time"] = end_time
        if extra_where:
            conditions.append(extra_where)

        sql = f"SELECT * FROM {table} WHERE {' AND '.join(conditions)}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {limit}"

        return sql, params

    def execute(self, sql: str, params: Optional[dict] = None) -> QueryResult:
        sql_params = []
        if params:
            for key, value in params.items():
                if isinstance(value, int):
                    sql_params.append({"name": key, "value": {"longValue": value}})
                elif isinstance(value, float):
                    sql_params.append({"name": key, "value": {"doubleValue": value}})
                else:
                    sql_params.append({"name": key, "value": {"stringValue": str(value)}})

        response = self.rds_data.execute_statement(
            resourceArn=self.cluster_arn,
            secretArn=self.secret_arn,
            database=self.database,
            sql=f"/* source=dbops-agent */ {sql}",
            parameters=sql_params,
            includeResultMetadata=True,
        )

        columns = [col["name"] for col in response.get("columnMetadata", [])]
        rows = []
        for record in response.get("records", []):
            row = {}
            for i, field in enumerate(record):
                col_name = columns[i] if i < len(columns) else f"col_{i}"
                if "stringValue" in field:
                    row[col_name] = field["stringValue"]
                elif "longValue" in field:
                    row[col_name] = field["longValue"]
                elif "doubleValue" in field:
                    row[col_name] = field["doubleValue"]
                elif "booleanValue" in field:
                    row[col_name] = field["booleanValue"]
                elif "isNull" in field:
                    row[col_name] = None
                else:
                    row[col_name] = str(field)
            rows.append(row)

        return QueryResult(columns=columns, rows=rows, row_count=len(rows))
```

- [ ] **Step 4: Add mcp-servers to Python path and run tests**

Add to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = [".", "mcp-servers"]
```

Run: `pytest tests/unit/mcp_servers/test_cache_client.py -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/shared/ tests/ pyproject.toml
git commit -m "feat: add shared MCP cache client with Data API integration"
```

---

## Task 5: Performance MCP Server — get_top_queries

**Files:**
- Create: `mcp-servers/performance/tools/__init__.py`
- Create: `mcp-servers/performance/tools/top_queries.py`
- Test: `tests/unit/mcp_servers/performance/test_top_queries.py`

- [ ] **Step 1: Write failing test**

```python
# tests/unit/mcp_servers/performance/test_top_queries.py
from unittest.mock import MagicMock
from mcp_servers.performance.tools.top_queries import get_top_queries_impl
from mcp_servers.shared.models import QueryResult


def test_get_top_queries_returns_sorted_results():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["query_hash", "query_text", "calls", "total_time_ms", "mean_time_ms"],
        rows=[
            {"query_hash": "abc", "query_text": "SELECT * FROM orders", "calls": 100, "total_time_ms": 5000.0, "mean_time_ms": 50.0},
            {"query_hash": "def", "query_text": "SELECT * FROM users", "calls": 200, "total_time_ms": 3000.0, "mean_time_ms": 15.0},
        ],
        row_count=2,
    )

    result = get_top_queries_impl(mock_cache, cluster_id="prod-pg-1", sort_by="total_time", limit=10)

    assert result["row_count"] == 2
    assert len(result["queries"]) == 2
    assert result["queries"][0]["query_hash"] == "abc"
    mock_cache.execute.assert_called_once()


def test_get_top_queries_with_calls_sort():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)

    result = get_top_queries_impl(mock_cache, cluster_id="prod-pg-1", sort_by="calls", limit=5)

    call_args = mock_cache.execute.call_args
    assert "calls DESC" in call_args[0][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/mcp_servers/performance/test_top_queries.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement get_top_queries**

```python
# mcp-servers/performance/tools/__init__.py
```

```python
# mcp-servers/performance/tools/top_queries.py
from mcp_servers.shared.cache_client import CacheClient

SORT_COLUMNS = {
    "total_time": "total_time_ms DESC",
    "calls": "calls DESC",
    "mean_time": "mean_time_ms DESC",
    "rows": "rows_returned DESC",
}


def get_top_queries_impl(
    cache: CacheClient,
    cluster_id: str,
    sort_by: str = "total_time",
    limit: int = 10,
    start_time: str = None,
    end_time: str = None,
) -> dict:
    order = SORT_COLUMNS.get(sort_by, "total_time_ms DESC")
    sql, params = cache._build_query(
        table="query_stats",
        cluster_id=cluster_id,
        time_column="snapshot_time" if start_time else None,
        start_time=start_time,
        end_time=end_time,
        order_by=order,
        limit=limit,
    )
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "sort_by": sort_by,
        "row_count": result.row_count,
        "queries": result.rows,
    }
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/mcp_servers/performance/test_top_queries.py -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/performance/tools/ tests/unit/mcp_servers/performance/
git commit -m "feat: add get_top_queries tool for Performance MCP Server"
```

---

## Task 6: Performance MCP Server — remaining tools

**Files:**
- Create: `mcp-servers/performance/tools/pi_metrics.py`
- Create: `mcp-servers/performance/tools/slow_queries.py`
- Create: `mcp-servers/performance/tools/compare_periods.py`
- Test: `tests/unit/mcp_servers/performance/test_pi_metrics.py`
- Test: `tests/unit/mcp_servers/performance/test_slow_queries.py`
- Test: `tests/unit/mcp_servers/performance/test_compare_periods.py`

This task follows the same TDD pattern as Task 5 for each tool. Each tool:
1. Write failing test with mocked CacheClient
2. Implement the `_impl` function
3. Run tests to verify pass

- [ ] **Step 1: Implement and test pi_metrics**

```python
# mcp-servers/performance/tools/pi_metrics.py
from mcp_servers.shared.cache_client import CacheClient


def get_pi_metrics_impl(
    cache: CacheClient,
    cluster_id: str,
    metric_type: str = "aas",
    start_time: str = None,
    end_time: str = None,
) -> dict:
    sql, params = cache._build_query(
        table="metric_snapshots",
        cluster_id=cluster_id,
        time_column="ts",
        start_time=start_time,
        end_time=end_time,
        extra_where=f"metric_type = :metric_type" if metric_type else None,
        order_by="ts ASC",
    )
    if metric_type:
        params["metric_type"] = metric_type
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "metric_type": metric_type,
        "data_points": result.rows,
        "count": result.row_count,
    }
```

```python
# tests/unit/mcp_servers/performance/test_pi_metrics.py
from unittest.mock import MagicMock
from mcp_servers.performance.tools.pi_metrics import get_pi_metrics_impl
from mcp_servers.shared.models import QueryResult


def test_get_pi_metrics_filters_by_type():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["ts", "value"], rows=[{"ts": "2026-05-01T00:00:00Z", "value": 3.2}], row_count=1
    )
    result = get_pi_metrics_impl(mock_cache, cluster_id="prod-pg-1", metric_type="aas")
    assert result["metric_type"] == "aas"
    assert result["count"] == 1
```

- [ ] **Step 2: Implement and test slow_queries**

```python
# mcp-servers/performance/tools/slow_queries.py
from mcp_servers.shared.cache_client import CacheClient


def get_slow_queries_impl(
    cache: CacheClient,
    cluster_id: str,
    threshold_ms: float = 1000.0,
    limit: int = 20,
    start_time: str = None,
    end_time: str = None,
) -> dict:
    sql, params = cache._build_query(
        table="slow_queries",
        cluster_id=cluster_id,
        time_column="ts",
        start_time=start_time,
        end_time=end_time,
        extra_where="execution_time_ms >= :threshold_ms",
        order_by="execution_time_ms DESC",
        limit=limit,
    )
    params["threshold_ms"] = threshold_ms
    result = cache.execute(sql, params)
    return {
        "cluster_id": cluster_id,
        "threshold_ms": threshold_ms,
        "row_count": result.row_count,
        "queries": result.rows,
    }
```

```python
# tests/unit/mcp_servers/performance/test_slow_queries.py
from unittest.mock import MagicMock
from mcp_servers.performance.tools.slow_queries import get_slow_queries_impl
from mcp_servers.shared.models import QueryResult


def test_get_slow_queries_filters_by_threshold():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(columns=[], rows=[], row_count=0)
    result = get_slow_queries_impl(mock_cache, cluster_id="prod-pg-1", threshold_ms=500.0)
    call_args = mock_cache.execute.call_args
    assert "execution_time_ms >= :threshold_ms" in call_args[0][0]
    assert call_args[0][1]["threshold_ms"] == 500.0
```

- [ ] **Step 3: Implement and test compare_periods**

```python
# mcp-servers/performance/tools/compare_periods.py
from mcp_servers.shared.cache_client import CacheClient


def compare_periods_impl(
    cache: CacheClient,
    cluster_id: str,
    period_a_start: str,
    period_a_end: str,
    period_b_start: str,
    period_b_end: str,
    metric_type: str = "aas",
) -> dict:
    def get_avg(start: str, end: str) -> dict:
        sql = """
            SELECT AVG(value) as avg_value, MAX(value) as max_value, MIN(value) as min_value,
                   COUNT(*) as sample_count
            FROM metric_snapshots
            WHERE cluster_id = :cluster_id AND metric_type = :metric_type
              AND ts >= :start_time AND ts < :end_time
        """
        params = {
            "cluster_id": cluster_id,
            "metric_type": metric_type,
            "start_time": start,
            "end_time": end,
        }
        result = cache.execute(sql, params)
        return result.rows[0] if result.rows else {}

    period_a = get_avg(period_a_start, period_a_end)
    period_b = get_avg(period_b_start, period_b_end)

    return {
        "cluster_id": cluster_id,
        "metric_type": metric_type,
        "period_a": {"start": period_a_start, "end": period_a_end, **period_a},
        "period_b": {"start": period_b_start, "end": period_b_end, **period_b},
    }
```

```python
# tests/unit/mcp_servers/performance/test_compare_periods.py
from unittest.mock import MagicMock, call
from mcp_servers.performance.tools.compare_periods import compare_periods_impl
from mcp_servers.shared.models import QueryResult


def test_compare_periods_calls_twice():
    mock_cache = MagicMock()
    mock_cache.execute.return_value = QueryResult(
        columns=["avg_value", "max_value", "min_value", "sample_count"],
        rows=[{"avg_value": 3.5, "max_value": 8.0, "min_value": 0.5, "sample_count": 100}],
        row_count=1,
    )
    result = compare_periods_impl(
        mock_cache, "prod-pg-1",
        "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z",
        "2026-05-07T00:00:00Z", "2026-05-08T00:00:00Z",
    )
    assert mock_cache.execute.call_count == 2
    assert "period_a" in result
    assert "period_b" in result
```

- [ ] **Step 4: Run all performance tool tests**

Run: `pytest tests/unit/mcp_servers/performance/ -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/performance/tools/ tests/unit/mcp_servers/performance/
git commit -m "feat: add pi_metrics, slow_queries, compare_periods tools"
```

---

## Task 7: Performance MCP Server — Lambda Handler

**Files:**
- Create: `mcp-servers/performance/handler.py`
- Create: `mcp-servers/performance/requirements.txt`

- [ ] **Step 1: Write MCP handler that exposes tools**

```python
# mcp-servers/performance/handler.py
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.cache_client import CacheClient
from tools.top_queries import get_top_queries_impl
from tools.pi_metrics import get_pi_metrics_impl
from tools.slow_queries import get_slow_queries_impl
from tools.compare_periods import compare_periods_impl

cache = CacheClient()

TOOLS = {
    "get_top_queries": {
        "impl": get_top_queries_impl,
        "description": "Get top-N queries from Aurora PG Cache sorted by total time, calls, or mean time",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string", "description": "Target Aurora cluster ID"},
                "sort_by": {"type": "string", "enum": ["total_time", "calls", "mean_time", "rows"], "default": "total_time"},
                "limit": {"type": "integer", "default": 10},
                "start_time": {"type": "string", "description": "ISO 8601 start time"},
                "end_time": {"type": "string", "description": "ISO 8601 end time"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_pi_metrics": {
        "impl": get_pi_metrics_impl,
        "description": "Get Performance Insights metrics (AAS, wait events, counter metrics) from cache",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "metric_type": {"type": "string", "enum": ["aas", "cpu", "connections", "iops", "wait_events"], "default": "aas"},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["cluster_id"],
        },
    },
    "get_slow_queries": {
        "impl": get_slow_queries_impl,
        "description": "Get slow queries exceeding threshold from cache (MySQL: slow query log, PG: pg_stat_statements)",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "threshold_ms": {"type": "number", "default": 1000.0},
                "limit": {"type": "integer", "default": 20},
                "start_time": {"type": "string"},
                "end_time": {"type": "string"},
            },
            "required": ["cluster_id"],
        },
    },
    "compare_periods": {
        "impl": compare_periods_impl,
        "description": "Compare metrics between two time periods for trend analysis",
        "input_schema": {
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
                "period_a_start": {"type": "string"},
                "period_a_end": {"type": "string"},
                "period_b_start": {"type": "string"},
                "period_b_end": {"type": "string"},
                "metric_type": {"type": "string", "default": "aas"},
            },
            "required": ["cluster_id", "period_a_start", "period_a_end", "period_b_start", "period_b_end"],
        },
    },
}


def lambda_handler(event, context):
    method = event.get("method")

    if method == "tools/list":
        tools_list = []
        for name, tool in TOOLS.items():
            tools_list.append({
                "name": name,
                "description": tool["description"],
                "inputSchema": tool["input_schema"],
            })
        return {"tools": tools_list}

    if method == "tools/call":
        tool_name = event.get("params", {}).get("name")
        arguments = event.get("params", {}).get("arguments", {})

        if tool_name not in TOOLS:
            return {"error": f"Unknown tool: {tool_name}"}

        impl = TOOLS[tool_name]["impl"]
        result = impl(cache, **arguments)
        return {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}

    return {"error": f"Unknown method: {method}"}
```

```txt
# mcp-servers/performance/requirements.txt
boto3>=1.35.0
psycopg2-binary>=2.9.0
```

- [ ] **Step 2: Commit**

```bash
git add mcp-servers/performance/handler.py mcp-servers/performance/requirements.txt
git commit -m "feat: add Performance MCP Server Lambda handler with 4 tools"
```

---

## Task 8: ETL Collector Lambda

**Files:**
- Create: `data-pipeline/etl_collector/handler.py`
- Create: `data-pipeline/etl_collector/collectors/__init__.py`
- Create: `data-pipeline/etl_collector/collectors/pi_collector.py`
- Create: `data-pipeline/etl_collector/collectors/stats_collector.py`
- Create: `data-pipeline/etl_collector/collectors/meta_collector.py`
- Create: `data-pipeline/etl_collector/requirements.txt`
- Test: `tests/unit/data_pipeline/test_etl_collector.py`

- [ ] **Step 1: Write failing test for meta_collector**

```python
# tests/unit/data_pipeline/__init__.py
```

```python
# tests/unit/data_pipeline/test_etl_collector.py
from unittest.mock import MagicMock, patch
from data_pipeline.etl_collector.collectors.meta_collector import collect_cluster_meta


def test_collect_cluster_meta_stores_in_cache():
    mock_rds = MagicMock()
    mock_rds.describe_db_clusters.return_value = {
        "DBClusters": [{
            "DBClusterIdentifier": "prod-pg-1",
            "Engine": "aurora-postgresql",
            "EngineVersion": "15.10",
            "Status": "available",
            "Endpoint": "prod-pg-1.cluster-xxx.ap-northeast-2.rds.amazonaws.com",
            "ReaderEndpoint": "prod-pg-1.cluster-ro-xxx.ap-northeast-2.rds.amazonaws.com",
            "AllocatedStorage": 100,
        }]
    }
    mock_cache_execute = MagicMock()

    result = collect_cluster_meta(
        rds_client=mock_rds,
        cache_execute=mock_cache_execute,
        cluster_id="prod-pg-1",
        account_id="123456789012",
        region="ap-northeast-2",
    )

    assert result["status"] == "available"
    mock_cache_execute.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. pytest tests/unit/data_pipeline/test_etl_collector.py -v`

Add `data-pipeline` to pythonpath in pyproject.toml:
```toml
pythonpath = [".", "mcp-servers", "data-pipeline"]
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement collectors**

```python
# data-pipeline/etl_collector/collectors/__init__.py
```

```python
# data-pipeline/etl_collector/collectors/meta_collector.py
def collect_cluster_meta(rds_client, cache_execute, cluster_id, account_id, region):
    response = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
    cluster = response["DBClusters"][0]

    sql = """
        INSERT INTO cluster_meta (cluster_id, account_id, region, engine, engine_version,
            status, endpoint, reader_endpoint, storage_size_gb, updated_at)
        VALUES (:cluster_id, :account_id, :region, :engine, :engine_version,
            :status, :endpoint, :reader_endpoint, :storage_size_gb, NOW())
        ON CONFLICT (cluster_id) DO UPDATE SET
            engine_version = EXCLUDED.engine_version,
            status = EXCLUDED.status,
            storage_size_gb = EXCLUDED.storage_size_gb,
            updated_at = NOW()
    """
    params = {
        "cluster_id": cluster_id,
        "account_id": account_id,
        "region": region,
        "engine": cluster["Engine"],
        "engine_version": cluster["EngineVersion"],
        "status": cluster["Status"],
        "endpoint": cluster.get("Endpoint", ""),
        "reader_endpoint": cluster.get("ReaderEndpoint", ""),
        "storage_size_gb": cluster.get("AllocatedStorage", 0),
    }
    cache_execute(sql, params)
    return {"cluster_id": cluster_id, "status": cluster["Status"]}
```

```python
# data-pipeline/etl_collector/collectors/pi_collector.py
from datetime import datetime, timedelta


def collect_pi_metrics(pi_client, cache_execute, cluster_resource_id, cluster_id):
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(minutes=5)

    response = pi_client.get_resource_metrics(
        ServiceType="RDS",
        Identifier=cluster_resource_id,
        MetricQueries=[
            {"Metric": "db.load.avg", "GroupBy": {"Group": "db.wait_event"}},
        ],
        StartTime=start_time,
        EndTime=end_time,
        PeriodInSeconds=60,
    )

    inserted = 0
    for metric_result in response.get("MetricList", []):
        for point in metric_result.get("DataPoints", []):
            sql = """
                INSERT INTO metric_snapshots (cluster_id, ts, metric_type, value, dimensions)
                VALUES (:cluster_id, :ts, :metric_type, :value, :dimensions::jsonb)
            """
            params = {
                "cluster_id": cluster_id,
                "ts": point["Timestamp"].isoformat(),
                "metric_type": "aas",
                "value": point.get("Value", 0.0),
                "dimensions": "{}",
            }
            cache_execute(sql, params)
            inserted += 1

    return {"cluster_id": cluster_id, "metrics_inserted": inserted}
```

```python
# data-pipeline/etl_collector/collectors/stats_collector.py
def collect_query_stats(rds_data_client, cache_execute, cluster_arn, secret_arn, cluster_id, database="dbops"):
    sql = """
        SELECT queryid::text as query_hash, query as query_text,
               calls, total_exec_time as total_time_ms,
               mean_exec_time as mean_time_ms, rows as rows_returned,
               shared_blks_hit, shared_blks_read
        FROM pg_stat_statements
        ORDER BY total_exec_time DESC
        LIMIT 100
    """

    response = rds_data_client.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=database,
        sql=f"/* source=dbops-agent */ {sql}",
        includeResultMetadata=True,
    )

    inserted = 0
    for record in response.get("records", []):
        insert_sql = """
            INSERT INTO query_stats (cluster_id, snapshot_time, query_hash, query_text,
                calls, total_time_ms, mean_time_ms, rows_returned, shared_blks_hit, shared_blks_read)
            VALUES (:cluster_id, NOW(), :query_hash, :query_text,
                :calls, :total_time_ms, :mean_time_ms, :rows_returned, :shared_blks_hit, :shared_blks_read)
        """
        fields = record
        params = {
            "cluster_id": cluster_id,
            "query_hash": fields[0].get("stringValue", ""),
            "query_text": fields[1].get("stringValue", ""),
            "calls": fields[2].get("longValue", 0),
            "total_time_ms": fields[3].get("doubleValue", 0.0),
            "mean_time_ms": fields[4].get("doubleValue", 0.0),
            "rows_returned": fields[5].get("longValue", 0),
            "shared_blks_hit": fields[6].get("longValue", 0),
            "shared_blks_read": fields[7].get("longValue", 0),
        }
        cache_execute(insert_sql, params)
        inserted += 1

    return {"cluster_id": cluster_id, "queries_collected": inserted}
```

- [ ] **Step 4: Implement Lambda handler**

```python
# data-pipeline/etl_collector/handler.py
import os
import json
import boto3
from collectors.meta_collector import collect_cluster_meta
from collectors.pi_collector import collect_pi_metrics
from collectors.stats_collector import collect_query_stats


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    clusters_table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    rds_data = boto3.client("rds-data")

    cache_cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    cache_secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    cache_db_name = os.environ.get("CACHE_DB_NAME", "dbops")

    def cache_execute(sql, params):
        sql_params = []
        for key, value in params.items():
            if isinstance(value, (int,)):
                sql_params.append({"name": key, "value": {"longValue": value}})
            elif isinstance(value, float):
                sql_params.append({"name": key, "value": {"doubleValue": value}})
            else:
                sql_params.append({"name": key, "value": {"stringValue": str(value)}})

        rds_data.execute_statement(
            resourceArn=cache_cluster_arn,
            secretArn=cache_secret_arn,
            database=cache_db_name,
            sql=f"/* source=dbops-etl */ {sql}",
            parameters=sql_params,
        )

    response = clusters_table.scan()
    clusters = response.get("Items", [])
    results = []

    for cluster in clusters:
        cluster_id = cluster["cluster_id"]
        account_id = cluster.get("account_id", "")
        region = cluster.get("region", os.environ.get("AWS_REGION", "ap-northeast-2"))

        rds_client = boto3.client("rds", region_name=region)
        pi_client = boto3.client("pi", region_name=region)

        meta = collect_cluster_meta(rds_client, cache_execute, cluster_id, account_id, region)
        pi = collect_pi_metrics(pi_client, cache_execute, cluster.get("resource_id", ""), cluster_id)

        results.append({"cluster_id": cluster_id, "meta": meta, "pi": pi})

    return {"statusCode": 200, "body": json.dumps({"collected": len(results)})}
```

```txt
# data-pipeline/etl_collector/requirements.txt
boto3>=1.35.0
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/data_pipeline/ -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add data-pipeline/ tests/unit/data_pipeline/ pyproject.toml
git commit -m "feat: add ETL collector Lambda with PI, stats, and meta collectors"
```

---

## Task 9: Strands Agent + Dockerfile

**Files:**
- Create: `agent/server.py`
- Create: `agent/prompts/__init__.py`
- Create: `agent/prompts/system_prompt.py`
- Create: `agent/prompts/cheatsheet.py`
- Create: `agent/requirements.txt`
- Create: `agent/Dockerfile`

- [ ] **Step 1: Write system prompt with cheatsheet**

```python
# agent/prompts/__init__.py
```

```python
# agent/prompts/cheatsheet.py
AURORA_CHEATSHEET = """
## Aurora 핵심 파라미터 (Quick Reference)

### PostgreSQL
- shared_buffers: 인스턴스 메모리의 25-40%. Aurora는 자동 관리하지만 확인 필요.
- work_mem: 정렬/해시 작업 메모리. 기본 4MB, 복잡 쿼리 시 16-64MB 고려.
- maintenance_work_mem: VACUUM/INDEX 작업용. 기본 64MB, 대형 테이블 시 256MB-1GB.
- effective_cache_size: 쿼리 플래너 힌트. 인스턴스 메모리의 75%.
- max_connections: 인스턴스 클래스별 상이. db.r6g.large=1600, db.r6g.xlarge=3200.
- idle_in_transaction_session_timeout: 유휴 트랜잭션 타임아웃. 권장 30초-5분.

### MySQL
- innodb_buffer_pool_size: Aurora는 자동 관리. 75% of RAM.
- max_connections: 기본 GREATEST({DBInstanceClassMemory/9531392}, 5000).
- innodb_lock_wait_timeout: 락 대기 시간. 기본 50초. 긴 트랜잭션 시 조정.
- slow_query_log: 1로 설정하여 활성화. long_query_time과 함께 사용.
- long_query_time: 슬로우 쿼리 기준. 기본 10초, 권장 1-2초.

## 진단 워크플로
1. 성능 저하 → PI 메트릭(AAS) 확인 → Top Wait Events 식별
2. Wait Event가 IO → 인덱스 확인 → EXPLAIN 분석 → 인덱스 추천
3. Wait Event가 Lock → pg_locks/innodb_lock_waits 확인 → Blocking 쿼리 식별
4. Wait Event가 CPU → Top SQL 확인 → 쿼리 최적화

## 위험 작업 판단 기준
- DROP/TRUNCATE: 항상 위험. 롤백 불가.
- ALTER TABLE (대형 테이블): 온라인 DDL 가능 여부 확인 필요.
- 파라미터 변경 (static): 재시작 필요. 점검 윈도우에서 수행.
- 파라미터 변경 (dynamic): 즉시 적용. 영향 범위 확인 후 수행.
"""
```

```python
# agent/prompts/system_prompt.py
from agent.prompts.cheatsheet import AURORA_CHEATSHEET

def build_system_prompt() -> str:
    return f"""당신은 DBA를 위한 AI 데이터베이스 운영 전문가입니다.
Amazon Aurora MySQL/PostgreSQL 클러스터의 성능 분석, 장애 진단, 운영 자동화를 돕습니다.

## 규칙
1. 모든 분석은 데이터에 기반합니다. 추측하지 마세요.
2. 도구를 호출하여 실제 데이터를 확인한 후 답변하세요.
3. 변경 작업(DDL, DML, 파라미터 변경)은 반드시 사용자 승인이 필요합니다.
4. 위험한 작업은 영향 분석과 롤백 계획을 먼저 제시하세요.
5. 한국어로 답변하세요.

## 지식 검색 우선순위
1. 아래 치트시트를 먼저 확인
2. 상세 문서가 필요하면 retrieve 도구 사용 (Bedrock KB)
3. KB 결과가 부족하거나 "최신", "새로운", "업데이트" 키워드가 있으면 AWS Knowledge MCP로 확인

{AURORA_CHEATSHEET}
"""
```

- [ ] **Step 2: Write agent server**

```python
# agent/server.py
import os
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client

from prompts.system_prompt import build_system_prompt

try:
    from strands_tools import retrieve
    HAS_RETRIEVE = True
except ImportError:
    HAS_RETRIEVE = False


def create_agent():
    model = BedrockModel(
        model_id=os.environ.get("AGENT_MODEL_ID", "anthropic.claude-sonnet-4-20250514-v1:0"),
        region_name=os.environ.get("AWS_REGION", "ap-northeast-2"),
    )

    gateway_id = os.environ.get("GATEWAY_ID", "")
    region = os.environ.get("AWS_REGION", "ap-northeast-2")
    gateway_url = f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"

    gateway_client = MCPClient(lambda: streamablehttp_client(gateway_url))

    tools = []
    if HAS_RETRIEVE:
        tools.append(retrieve)

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
```

- [ ] **Step 3: Write Dockerfile**

```dockerfile
# agent/Dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "server.py"]
```

```txt
# agent/requirements.txt
strands-agents>=1.0.0
strands-agents-tools>=0.1.0
boto3>=1.35.0
```

- [ ] **Step 4: Commit**

```bash
git add agent/
git commit -m "feat: add Strands Agent with system prompt, cheatsheet, and Dockerfile"
```

---

## Task 10: Agent + Frontend CDK Stacks (Skeleton)

**Files:**
- Create: `cdk/stacks/agent_stack.py`
- Create: `cdk/stacks/frontend_stack.py`

- [ ] **Step 1: Write Agent Stack**

```python
# cdk/stacks/agent_stack.py
import aws_cdk as cdk
from aws_cdk import (
    aws_apigateway as apigw,
    aws_lambda as lambda_,
    aws_lambda_python_alpha as lambda_python,
)
from constructs import Construct
from config.settings import Settings


class AgentStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, data, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # REST API for dashboard
        self.api = apigw.RestApi(
            self, "RestApi",
            rest_api_name=f"dbops-{Settings.ENV}-api",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
            ),
        )

        # Dashboard API Lambda
        dashboard_lambda = lambda_python.PythonFunction(
            self, "DashboardApi",
            entry="api/dashboard",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            timeout=cdk.Duration.seconds(30),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(dashboard_lambda)
        data.cache_db.grant_data_api_access(dashboard_lambda)

        dashboard_resource = self.api.root.add_resource("api").add_resource("dashboard")
        cluster_resource = dashboard_resource.add_resource("{cluster_id}")
        cluster_resource.add_method("GET", apigw.LambdaIntegration(dashboard_lambda))

        # Clusters API Lambda
        clusters_lambda = lambda_python.PythonFunction(
            self, "ClustersApi",
            entry="api/clusters",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            timeout=cdk.Duration.seconds(30),
            environment={
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        foundation.clusters_table.grant_read_write_data(clusters_lambda)

        clusters_resource = self.api.root.add_resource("api").add_resource("clusters")
        clusters_resource.add_method("GET", apigw.LambdaIntegration(clusters_lambda))
        clusters_resource.add_method("POST", apigw.LambdaIntegration(clusters_lambda))

        cdk.CfnOutput(self, "ApiUrl", value=self.api.url)
```

Note: AgentCore Runtime and Gateway are not yet available as CDK L2 constructs. These will be configured via AgentCore CLI or custom CloudFormation resources. The stack above provides the REST API portion.

- [ ] **Step 2: Write Frontend Stack**

```python
# cdk/stacks/frontend_stack.py
import aws_cdk as cdk
from aws_cdk import (
    aws_s3 as s3,
    aws_s3_deployment as s3_deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct
from config.settings import Settings


class FrontendStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, agent, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self, "FrontendBucket",
            bucket_name=f"dbops-{Settings.ENV}-frontend-{Settings.ACCOUNT_ID}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        distribution = cloudfront.Distribution(
            self, "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )

        s3_deploy.BucketDeployment(
            self, "DeployFrontend",
            sources=[s3_deploy.Source.asset("../frontend/out")],
            destination_bucket=bucket,
            distribution=distribution,
        )

        cdk.CfnOutput(self, "DistributionUrl", value=f"https://{distribution.distribution_domain_name}")
```

- [ ] **Step 3: Commit**

```bash
git add cdk/stacks/agent_stack.py cdk/stacks/frontend_stack.py
git commit -m "feat(cdk): add Agent stack (REST API) and Frontend stack (CloudFront + S3)"
```

---

## Task 11: REST API Lambdas

**Files:**
- Create: `api/dashboard/handler.py`
- Create: `api/clusters/handler.py`

- [ ] **Step 1: Write dashboard API**

```python
# api/dashboard/handler.py
import json
import os
import boto3


def lambda_handler(event, context):
    cluster_id = event.get("pathParameters", {}).get("cluster_id")
    if not cluster_id:
        return {"statusCode": 400, "body": json.dumps({"error": "cluster_id required"})}

    rds_data = boto3.client("rds-data")
    cluster_arn = os.environ["CACHE_DB_CLUSTER_ARN"]
    secret_arn = os.environ["CACHE_DB_SECRET_ARN"]
    database = os.environ.get("CACHE_DB_NAME", "dbops")

    def query(sql, params=None):
        sql_params = []
        if params:
            for k, v in params.items():
                sql_params.append({"name": k, "value": {"stringValue": str(v)}})
        resp = rds_data.execute_statement(
            resourceArn=cluster_arn, secretArn=secret_arn, database=database,
            sql=f"/* source=dbops-dashboard */ {sql}", parameters=sql_params,
            includeResultMetadata=True,
        )
        cols = [c["name"] for c in resp.get("columnMetadata", [])]
        rows = []
        for rec in resp.get("records", []):
            row = {}
            for i, f in enumerate(rec):
                col = cols[i] if i < len(cols) else f"col_{i}"
                for typ in ("stringValue", "longValue", "doubleValue", "booleanValue"):
                    if typ in f:
                        row[col] = f[typ]
                        break
                else:
                    row[col] = None
            rows.append(row)
        return rows

    meta = query("SELECT * FROM cluster_meta WHERE cluster_id = :cid", {"cid": cluster_id})
    recent_metrics = query(
        "SELECT metric_type, AVG(value) as avg_val, MAX(value) as max_val "
        "FROM metric_snapshots WHERE cluster_id = :cid AND ts > NOW() - INTERVAL '1 hour' "
        "GROUP BY metric_type",
        {"cid": cluster_id},
    )
    top_queries = query(
        "SELECT query_hash, query_text, calls, total_time_ms, mean_time_ms "
        "FROM query_stats WHERE cluster_id = :cid AND snapshot_time > NOW() - INTERVAL '1 hour' "
        "ORDER BY total_time_ms DESC LIMIT 5",
        {"cid": cluster_id},
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps({
            "cluster": meta[0] if meta else None,
            "metrics": recent_metrics,
            "top_queries": top_queries,
        }, default=str),
    }
```

- [ ] **Step 2: Write clusters API**

```python
# api/clusters/handler.py
import json
import os
import boto3
from datetime import datetime


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["CLUSTERS_TABLE"])
    method = event.get("httpMethod", "GET")

    if method == "GET":
        response = table.scan()
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(response.get("Items", []), default=str),
        }

    if method == "POST":
        body = json.loads(event.get("body", "{}"))
        required = ["cluster_id", "account_id", "region"]
        for field in required:
            if field not in body:
                return {"statusCode": 400, "body": json.dumps({"error": f"{field} required"})}

        table.put_item(Item={
            "cluster_id": body["cluster_id"],
            "account_id": body["account_id"],
            "region": body["region"],
            "engine": body.get("engine", "aurora-postgresql"),
            "spoke_role_arn": body.get("spoke_role_arn", ""),
            "registered_at": datetime.utcnow().isoformat(),
        })
        return {
            "statusCode": 201,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"status": "registered", "cluster_id": body["cluster_id"]}),
        }

    return {"statusCode": 405, "body": json.dumps({"error": "Method not allowed"})}
```

- [ ] **Step 3: Commit**

```bash
git add api/
git commit -m "feat: add REST API Lambdas for dashboard and cluster management"
```

---

## Task 12: Frontend Initialization

**Files:**
- Create: `frontend/` (Next.js project)

- [ ] **Step 1: Initialize Next.js project**

```bash
cd frontend
npx create-next-app@latest . --typescript --tailwind --eslint --app --src-dir --no-import-alias
```

- [ ] **Step 2: Install dependencies**

```bash
cd frontend
npm install @tanstack/react-query class-variance-authority clsx tailwind-merge lucide-react
```

- [ ] **Step 3: Configure static export**

Edit `frontend/next.config.ts`:
```typescript
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "export",
  images: { unoptimized: true },
};

export default nextConfig;
```

- [ ] **Step 4: Create auth utility stub**

```typescript
// frontend/src/lib/auth.ts
const COGNITO_DOMAIN = process.env.NEXT_PUBLIC_COGNITO_DOMAIN || "";
const CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || "";
const REDIRECT_URI = process.env.NEXT_PUBLIC_REDIRECT_URI || "http://localhost:3000/callback";

export function getLoginUrl(): string {
  return `https://${COGNITO_DOMAIN}.auth.ap-northeast-2.amazoncognito.com/login?client_id=${CLIENT_ID}&response_type=code&scope=openid+profile&redirect_uri=${encodeURIComponent(REDIRECT_URI)}`;
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("dbops_token");
}

export function setToken(token: string): void {
  localStorage.setItem("dbops_token", token);
}
```

- [ ] **Step 5: Create API client**

```typescript
// frontend/src/lib/api-client.ts
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:3001";

export async function fetchDashboard(clusterId: string) {
  const res = await fetch(`${API_BASE}/api/dashboard/${clusterId}`);
  if (!res.ok) throw new Error(`Dashboard fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchClusters() {
  const res = await fetch(`${API_BASE}/api/clusters`);
  if (!res.ok) throw new Error(`Clusters fetch failed: ${res.status}`);
  return res.json();
}

export async function registerCluster(data: {
  cluster_id: string;
  account_id: string;
  region: string;
  engine?: string;
  spoke_role_arn?: string;
}) {
  const res = await fetch(`${API_BASE}/api/clusters`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Register failed: ${res.status}`);
  return res.json();
}
```

- [ ] **Step 6: Create SSE client for AgentCore**

```typescript
// frontend/src/lib/agentcore-sse.ts
import { getToken } from "./auth";

const RUNTIME_URL = process.env.NEXT_PUBLIC_AGENTCORE_RUNTIME_URL || "";

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  toolCalls?: { name: string; status: "running" | "done"; result?: string }[];
}

export function streamChat(
  message: string,
  clusterId: string,
  onToken: (token: string) => void,
  onToolCall: (name: string, status: string) => void,
  onDone: () => void,
  onError: (error: Error) => void,
): AbortController {
  const controller = new AbortController();
  const token = getToken();

  fetch(`${RUNTIME_URL}/invoke`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      message,
      context: { cluster_id: clusterId },
    }),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) throw new Error(`SSE failed: ${response.status}`);
      const reader = response.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const text = decoder.decode(value, { stream: true });
        const lines = text.split("\n");
        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const data = line.slice(6);
            if (data === "[DONE]") {
              onDone();
              return;
            }
            try {
              const parsed = JSON.parse(data);
              if (parsed.type === "text") onToken(parsed.content);
              if (parsed.type === "tool_use") onToolCall(parsed.name, parsed.status);
            } catch {
              onToken(data);
            }
          }
        }
      }
      onDone();
    })
    .catch((err) => {
      if (err.name !== "AbortError") onError(err);
    });

  return controller;
}
```

- [ ] **Step 7: Commit**

```bash
git add frontend/
git commit -m "feat: initialize Next.js frontend with auth, API client, and SSE client"
```

---

## Task 13: Frontend — Chat Page

**Files:**
- Create: `frontend/src/components/chat/chat-panel.tsx`
- Create: `frontend/src/components/chat/message-list.tsx`
- Create: `frontend/src/components/chat/tool-status.tsx`
- Create: `frontend/src/app/chat/page.tsx`

> Note: UI components here are functional stubs. Full design will be implemented via Claude Design → Claude Code handoff workflow in a separate iteration.

- [ ] **Step 1: Build chat components and page**

This step creates the functional chat interface. Detailed implementation code should follow the Claude Design handoff bundle for visual styling. The structure:

- `chat-panel.tsx` — Main chat container with input, message list, cluster selector
- `message-list.tsx` — Renders messages with markdown and tool call status
- `tool-status.tsx` — Shows active tool execution (name + spinner)
- `app/chat/page.tsx` — Page wrapper

- [ ] **Step 2: Verify dev server starts**

Run: `cd frontend && npm run dev`
Expected: Next.js dev server starts at http://localhost:3000, chat page renders at /chat

- [ ] **Step 3: Commit**

```bash
git add frontend/src/
git commit -m "feat: add Chat page with SSE streaming and tool status display"
```

---

## Task 14: Frontend — Dashboard Page

**Files:**
- Create: `frontend/src/components/design-system/metric-card.tsx`
- Create: `frontend/src/components/design-system/status-badge.tsx`
- Create: `frontend/src/components/dashboard/cluster-overview.tsx`
- Create: `frontend/src/components/dashboard/aas-chart.tsx`
- Create: `frontend/src/app/dashboard/page.tsx`

> Note: Same as Task 13 — functional stubs. Full design via Claude Design handoff.

- [ ] **Step 1: Build dashboard components and page**

- `metric-card.tsx` — Reusable card showing label + value + trend
- `status-badge.tsx` — Healthy/Warning/Critical badge
- `cluster-overview.tsx` — Grid of cluster cards with health status
- `aas-chart.tsx` — Time-series chart for AAS metrics
- `app/dashboard/page.tsx` — Page with TanStack Query polling (5s refresh)

- [ ] **Step 2: Verify dashboard page renders**

Run: `cd frontend && npm run dev`
Navigate to http://localhost:3000/dashboard
Expected: Page renders (empty state since no API connected)

- [ ] **Step 3: Build static export**

Run: `cd frontend && npm run build`
Expected: Static export generated in `frontend/out/`

- [ ] **Step 4: Commit**

```bash
git add frontend/src/
git commit -m "feat: add Dashboard page with cluster overview and metric charts"
```

---

## Task 15: Knowledge Base Setup

**Files:**
- Create: `knowledge/aurora-docs/README.md`

- [ ] **Step 1: Create knowledge base placeholder**

```markdown
<!-- knowledge/aurora-docs/README.md -->
# Aurora Documentation for Bedrock KB

Place Aurora MySQL and PostgreSQL documentation files here.
These will be indexed by Bedrock Knowledge Bases with S3 Vectors backend.

## Recommended documents:
- Aurora PostgreSQL User Guide (key sections)
- Aurora MySQL User Guide (key sections)
- Parameter group reference
- Troubleshooting guide
- Performance Insights documentation
- Best practices guide

## Format
- PDF or Markdown files
- One document per file for better chunking
```

- [ ] **Step 2: Commit**

```bash
git add knowledge/
git commit -m "docs: add knowledge base placeholder for Bedrock KB ingestion"
```

---

## Task 16: Full Test Suite + Final Verification

- [ ] **Step 1: Run all unit tests**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS (at least 7 tests)

- [ ] **Step 2: Verify CDK synth for all stacks**

Run: `cd cdk && cdk synth --quiet`
Expected: All 4 stacks synthesize without errors

- [ ] **Step 3: Verify frontend builds**

Run: `cd frontend && npm run build`
Expected: Static export generated successfully

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "chore: Phase 1 MVP implementation complete

All core components implemented:
- CDK 4-stack infrastructure (Foundation, Data, Agent, Frontend)
- Performance MCP Server with 4 custom tools
- ETL Collector Lambda with PI, stats, meta collectors
- Strands Agent with system prompt and Aurora cheatsheet
- REST API Lambdas for dashboard and cluster management
- Next.js frontend with Chat and Dashboard pages
- Bedrock KB knowledge base placeholder

Confidence: high
Scope-risk: moderate
Not-tested: AgentCore Runtime/Gateway deployment (requires AWS account)
Not-tested: Cross-account IAM role assumption
Not-tested: End-to-end SSE streaming with real AgentCore Runtime"
```

---

## Post-Implementation Notes

### CDK Deploy Sequence
```bash
cd cdk
cdk bootstrap
cdk deploy dbops-dev-foundation
cdk deploy dbops-dev-data
# Run schema.sql against Aurora PG Cache via Data API
cdk deploy dbops-dev-agent
# Configure AgentCore Runtime and Gateway via agentcore CLI
cdk deploy dbops-dev-frontend
```

### AgentCore Setup (Manual, via CLI)
AgentCore Runtime and Gateway are not yet available as CDK L2 constructs. After deploying the Agent stack:
```bash
npm install -g @aws/agentcore
agentcore create --defaults
agentcore deploy
```

### What's Not Covered in Phase 1
- AgentCore Memory integration (Phase 2)
- Incident/Operations/Simulation MCP Servers (Phase 2-4)
- Approval workflow (Phase 3)
- Cross-account IAM (Phase 4)
- Advanced analytics tools (Phase 2+)
- Claude Design UI refinement (ongoing)
