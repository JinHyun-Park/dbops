# Requirements: Phase 1 — Performance Analysis Agent (MVP)

## Introduction
DBA가 자연어 대화로 Aurora MySQL/PostgreSQL 클러스터의 성능을 분석할 수 있는 MVP. 단일 계정, 단일 클러스터 대상.

## Requirements

### 1. CDK Foundation Infrastructure
**User Story:** As a deployer, I want to deploy the entire platform with `cdk deploy`, so that I can start using DBOps in my AWS account.

#### Acceptance Criteria
1.1. WHEN a user runs `cdk deploy --all` THE SYSTEM SHALL provision all required AWS resources
1.2. WHEN deployment completes THE SYSTEM SHALL output the Web UI URL and AgentCore Runtime endpoint
1.3. IF `cdk/config/settings.py` is not configured THEN THE SYSTEM SHALL fail with a clear error message indicating required fields

### 2. Data Collection Pipeline
**User Story:** As a DBA, I want the system to automatically collect performance data from my Aurora cluster, so that I can query historical metrics.

#### Acceptance Criteria
2.1. WHEN an Aurora cluster is registered THE SYSTEM SHALL begin collecting PI metrics every 1 minute
2.2. WHEN the ETL collector runs THE SYSTEM SHALL store pg_stat_statements snapshots every 5 minutes
2.3. WHEN the ETL collector runs THE SYSTEM SHALL store cluster metadata every 5 minutes
2.4. WHEN collected data is older than 7 days THE SYSTEM SHALL archive it according to retention policy
2.5. IF the target Aurora cluster is unreachable THEN THE SYSTEM SHALL log the error and retry on next interval

### 3. AI Chat Interface
**User Story:** As a DBA, I want to ask questions about my database performance in natural language, so that I can get instant analysis without writing queries.

#### Acceptance Criteria
3.1. WHEN a DBA sends a message THE SYSTEM SHALL stream the response token-by-token via SSE
3.2. WHEN the agent calls a tool THE SYSTEM SHALL display the tool name and status in the chat UI
3.3. WHEN the agent returns structured data (tables, metrics) THE SYSTEM SHALL render them as rich cards
3.4. WHEN a DBA selects a cluster from the dropdown THE SYSTEM SHALL scope all subsequent queries to that cluster
3.5. IF the SSE connection drops THEN THE SYSTEM SHALL auto-reconnect and restore conversation context

### 4. Slow Query Analysis
**User Story:** As a DBA, I want the AI to identify and analyze slow queries, so that I can optimize database performance.

#### Acceptance Criteria
4.1. WHEN a DBA asks about slow queries THE SYSTEM SHALL return the top-N queries sorted by total execution time
4.2. WHEN a DBA asks to explain a query THE SYSTEM SHALL execute EXPLAIN ANALYZE on the target Aurora cluster
4.3. WHEN EXPLAIN results contain Sequential Scans on large tables THE SYSTEM SHALL flag them as potential issues
4.4. WHEN a problematic query is identified THE SYSTEM SHALL recommend specific index changes with estimated impact

### 5. Performance Dashboard
**User Story:** As a DBA, I want a visual dashboard showing real-time cluster health, so that I can monitor performance at a glance.

#### Acceptance Criteria
5.1. WHEN a DBA opens the Dashboard page THE SYSTEM SHALL display all registered clusters with health status
5.2. WHEN a DBA selects a cluster THE SYSTEM SHALL show AAS chart, top queries, connection stats, and storage usage
5.3. WHEN dashboard data is requested THE SYSTEM SHALL query Aurora PG Cache directly (not through the AI agent)
5.4. WHEN dashboard metrics refresh THE SYSTEM SHALL complete the query within 500ms

### 6. Knowledge Base
**User Story:** As a DBA, I want the AI to reference Aurora documentation when answering questions, so that recommendations are based on official best practices.

#### Acceptance Criteria
6.1. WHEN the agent needs documentation context THE SYSTEM SHALL first check the system prompt cheatsheet
6.2. WHEN deeper reference is needed THE SYSTEM SHALL query Bedrock KB via the retrieve tool
6.3. WHEN KB results are insufficient and the query contains "latest" or "update" keywords THE SYSTEM SHALL fall back to AWS Docs MCP
6.4. WHEN the knowledge base is first deployed THE SYSTEM SHALL index Aurora MySQL and PostgreSQL documentation from the knowledge/ directory

### 7. Authentication
**User Story:** As an admin, I want users to authenticate before accessing the platform, so that only authorized DBAs can use it.

#### Acceptance Criteria
7.1. WHEN a user accesses the Web UI THE SYSTEM SHALL redirect to Cognito Hosted UI for login
7.2. WHEN authentication succeeds THE SYSTEM SHALL establish both REST API and AgentCore SSE sessions with the same JWT
7.3. IF a JWT token expires THEN THE SYSTEM SHALL silently refresh using the refresh token
