# Implementation Plan: Phase 1 — Performance Analysis Agent

- [ ] 1. Initialize project structure and CDK app
  - Create directory structure matching structure.md
  - Initialize CDK Python app with `cdk init`
  - Create `settings.example.py` with all configurable values
  - Set up Python virtual environment and requirements.txt
  - _Requirements: 1.1_

- [ ] 2. Implement Foundation CDK Stack
  - Create Cognito User Pool with PKCE client
  - Create DynamoDB tables (sessions, clusters)
  - Create IAM Hub Role for cross-account access
  - Create VPC (if needed for Lambda-Aurora connectivity)
  - _Requirements: 1.1, 7.1, 7.2_

- [ ] 3. Implement Data CDK Stack
  - Create Aurora PostgreSQL Serverless v2 (I/O-Optimized)
  - Run schema migrations (cluster_meta, metric_snapshots, query_stats, slow_queries)
  - Create S3 bucket for EXPLAIN plans and archives
  - Create Bedrock Knowledge Base with S3 Vectors backend
  - Ingest Aurora documentation from knowledge/ directory
  - _Requirements: 2.1, 6.4_

- [ ] 4. Build ETL Collector Lambda
  - Implement PI metrics collector (GetResourceMetrics API)
  - Implement pg_stat_statements collector (RDS Data API)
  - Implement cluster metadata collector (DescribeDBClusters)
  - Implement slow query log parser
  - Create EventBridge schedule (5-minute interval)
  - Add error handling with retry and logging
  - _Requirements: 2.1, 2.2, 2.3, 2.5_

- [ ] 5. Build Performance MCP Server
  - Implement `get_top_queries` tool with Aurora PG Cache queries
  - Implement `explain_query` tool with RDS Data API call + S3 storage
  - Implement `get_pi_metrics` tool with time-range filtering
  - Implement `recommend_index` tool with index_usage + query_stats join
  - Implement `get_slow_queries` tool with threshold filtering
  - Implement `compare_periods` tool with two-period aggregation
  - Package as Lambda deployment artifact
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 5.2_

- [ ] 6. Build shared MCP utilities
  - Implement `db_connector.py` with RDS Data API client and IAM role assumption
  - Implement `cache_client.py` with Aurora PG Cache connection pooling
  - Implement SQL audit comment injection (`/* source=dbops-agent */`)
  - _Requirements: 4.2_

- [ ] 7. Implement Agent CDK Stack
  - Create AgentCore Runtime with Cognito auth and SSE
  - Create AgentCore Gateway with Performance MCP Server as Lambda target
  - Enable Gateway Semantic Search
  - Configure Cedar Policy (READ-ONLY: SELECT and EXPLAIN only)
  - Create API Gateway HTTP API with Cognito JWT authorizer
  - Create REST API Lambdas (dashboard, clusters)
  - _Requirements: 1.1, 3.1, 5.3_

- [ ] 8. Build Strands Agent
  - Create `server.py` with BedrockModel + MCPClient + Gateway connection
  - Write system prompt with Aurora cheatsheet (Tier 1 knowledge)
  - Register Bedrock KB `retrieve` tool (Tier 2 knowledge)
  - Configure AgentCore Memory for session persistence
  - Build and push Docker container to ECR
  - _Requirements: 3.1, 3.4, 6.1, 6.2, 6.3_

- [ ] 9. Build REST API Lambdas
  - Implement `GET /api/clusters` — list registered clusters from DynamoDB
  - Implement `POST /api/clusters` — register new cluster with connection test
  - Implement `GET /api/dashboard/{cluster_id}` — aggregated metrics from Aurora PG Cache
  - Implement `GET /api/metrics/{cluster_id}` — time-series data for charts
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [ ] 10. Initialize Next.js frontend project
  - Set up Next.js 15 with App Router and Static Export
  - Configure Tailwind CSS and shadcn/ui
  - Define design tokens (colors, typography, spacing) from Claude Design handoff
  - Set up Cognito auth flow (login, callback, token refresh)
  - _Requirements: 7.1, 7.2, 7.3_

- [ ] 11. Build Chat page
  - Implement SSE client for AgentCore Runtime connection
  - Build message renderer with markdown and rich card support
  - Build tool execution status display (tool name, spinner, result)
  - Build cluster selector dropdown
  - Implement auto-reconnect on SSE disconnect
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 12. Build Dashboard page
  - Build cluster overview grid with health status cards
  - Build AAS time-series chart with wait event color coding
  - Build top queries table component
  - Build connection stats and storage usage panels
  - Implement TanStack Query polling (5-second refresh)
  - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [ ] 13. Implement Frontend CDK Stack
  - Create S3 bucket for static hosting
  - Create CloudFront distribution with S3 origin
  - Configure CORS for AgentCore Runtime SSE and API Gateway
  - Add build step to compile and upload frontend assets
  - _Requirements: 1.1, 1.2_

- [ ] 14. Write unit tests
  - Test each Performance MCP tool with mocked DB responses
  - Test ETL Collector with mocked AWS API responses
  - Test REST API Lambdas with mocked DynamoDB/Aurora responses
  - CDK snapshot tests for all stacks
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [ ] 15. Integration and E2E testing
  - Deploy to dev environment
  - Register a test Aurora cluster
  - Verify data collection pipeline (check Aurora PG Cache tables)
  - Test chat flow end-to-end (ask slow query question, verify streamed response)
  - Test dashboard rendering with real collected data
  - Verify <500ms dashboard query latency
  - _Requirements: 2.1, 3.1, 4.1, 5.4_
