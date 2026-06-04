import aws_cdk as cdk
import aws_cdk.aws_bedrock_agentcore_alpha as agentcore
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as integrations,
)
from aws_cdk import aws_bedrockagentcore as agentcore_cfn
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from config.settings import Settings
from constructs import Construct
from tool_definitions import incident_schema, operations_schema, performance_schema, simulation_schema


class AgentStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, data, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ===== MCP Server Lambdas =====

        perf_mcp_lambda = lambda_.Function(
            self, "PerformanceMCP",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="mcp_servers.performance.handler.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers"),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            vpc=data.vpc,
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(perf_mcp_lambda)
        data.cache_db.grant_data_api_access(perf_mcp_lambda)

        incident_mcp_lambda = lambda_.Function(
            self, "IncidentMCP",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="mcp_servers.incident.handler.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers"),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            vpc=data.vpc,
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(incident_mcp_lambda)
        data.cache_db.grant_data_api_access(incident_mcp_lambda)

        operations_mcp_lambda = lambda_.Function(
            self, "OperationsMCP",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="mcp_servers.operations.handler.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers"),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            vpc=data.vpc,
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                # request_approval tool writes here; FRONTEND_URL lets the
                # tool emit a working review link in its return payload.
                "APPROVALS_TABLE": foundation.approvals_table.table_name,
                "FRONTEND_URL": Settings.FRONTEND_URL,
            },
        )
        data.cache_db.secret.grant_read(operations_mcp_lambda)
        data.cache_db.grant_data_api_access(operations_mcp_lambda)
        foundation.clusters_table.grant_read_data(operations_mcp_lambda)
        foundation.approvals_table.grant_read_write_data(operations_mcp_lambda)
        # Allow agent-driven SQL against ANY registered Aurora cluster + admin actions.
        # All write paths still gate through approved=true in tool code.
        operations_mcp_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds-data:ExecuteStatement",
                "rds-data:BatchExecuteStatement",
                "secretsmanager:GetSecretValue",
                "rds:DescribeDBClusters",
                "rds:DescribeDBClusterParameterGroups",
                "rds:DescribeDBClusterParameters",
                "rds:ModifyDBClusterParameterGroup",
                "rds:ModifyDBCluster",
            ],
            resources=["*"],
        ))

        simulation_mcp_lambda = lambda_.Function(
            self, "SimulationMCP",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="mcp_servers.simulation.handler.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers"),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            vpc=data.vpc,
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(simulation_mcp_lambda)
        data.cache_db.grant_data_api_access(simulation_mcp_lambda)

        # ===== AgentCore Gateway =====

        self.gateway = agentcore.Gateway(
            self, "Gateway",
            gateway_name=f"dbops-{Settings.ENV}-gateway",
        )

        mcp_lambdas = {
            "performance": (perf_mcp_lambda, performance_schema()),
            "incident": (incident_mcp_lambda, incident_schema()),
            "operations": (operations_mcp_lambda, operations_schema()),
            "simulation": (simulation_mcp_lambda, simulation_schema()),
        }

        for name, (fn, schema) in mcp_lambdas.items():
            fn.grant_invoke(self.gateway.role)
            fn.add_permission(
                f"AgentCoreInvoke{name.title()}",
                principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                action="lambda:InvokeFunction",
                source_arn=self.gateway.gateway_arn,
            )

            target = agentcore_cfn.CfnGatewayTarget(
                self, f"{name.title()}Target",
                name=f"dbops-{Settings.ENV}-{name}-target",
                gateway_identifier=self.gateway.gateway_id,
                target_configuration=agentcore_cfn.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=agentcore_cfn.CfnGatewayTarget.McpTargetConfigurationProperty(
                        lambda_=agentcore_cfn.CfnGatewayTarget.McpLambdaTargetConfigurationProperty(
                            lambda_arn=fn.function_arn,
                            tool_schema=agentcore_cfn.CfnGatewayTarget.ToolSchemaProperty(
                                inline_payload=schema,
                            ),
                        ),
                    ),
                ),
                credential_provider_configurations=[
                    agentcore_cfn.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                        credential_provider_type="GATEWAY_IAM_ROLE",
                    ),
                ],
            )
            target.add_dependency(self.gateway.role.node.default_child)
            if self.gateway.role.node.try_find_child("DefaultPolicy"):
                target.add_dependency(self.gateway.role.node.find_child("DefaultPolicy").node.default_child)

        # ===== AgentCore Memory =====

        self.memory = agentcore.Memory(
            self, "Memory",
            memory_name=f"dbops_{Settings.ENV}_memory",
            memory_strategies=[
                agentcore.ManagedMemoryStrategy(
                    strategy_type=agentcore.MemoryStrategyType.SEMANTIC,
                    namespaces=["/users/{actorId}/facts"],
                    name="semantic",
                ),
                agentcore.ManagedMemoryStrategy(
                    strategy_type=agentcore.MemoryStrategyType.USER_PREFERENCE,
                    namespaces=["/users/{actorId}/preferences"],
                    name="preference",
                ),
                agentcore.ManagedMemoryStrategy(
                    strategy_type=agentcore.MemoryStrategyType.SUMMARIZATION,
                    namespaces=["/summaries/{actorId}/{sessionId}"],
                    name="summary",
                ),
            ],
        )

        # ===== AgentCore Runtime =====

        gateway_mcp_url = f"https://{self.gateway.gateway_id}.gateway.bedrock-agentcore.{Settings.REGION}.amazonaws.com/mcp"

        self.runtime = agentcore.Runtime(
            self, "Runtime",
            runtime_name=f"dbops_{Settings.ENV}_runtime",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_code_asset(
                entrypoint=["server.py"],
                path="../agent",
                runtime=agentcore.AgentCoreRuntime.PYTHON_3_12,
            ),
            environment_variables={
                "AGENT_MODEL_ID": Settings.AGENT_MODEL_ID,
                "AWS_REGION_OVERRIDE": Settings.REGION,
                "GATEWAY_MCP_URL": gateway_mcp_url,
            },
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_cognito(
                user_pool=foundation.user_pool,
                user_pool_clients=[foundation.user_pool_client],
            ),
        )

        # ===== REST API =====

        self.api = apigwv2.HttpApi(
            self, "HttpApi",
            api_name=f"dbops-{Settings.ENV}-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_headers=["content-type", "authorization"],
                allow_credentials=False,
            ),
        )
        # Throttle on the $default stage so a runaway client (or a
        # misconfigured polling loop) can't burn budget or take down
        # the rate-limited downstream (RDS Data API, Bedrock). HTTP API
        # has no built-in usage plans, so we set burst + steady-state
        # via CfnStage escape-hatch. The numbers are intentionally
        # generous — DBOps is a UI-driven app with low natural QPS;
        # anything above ~50 rps sustained is a bug, not a feature.
        cfn_stage = self.api.default_stage.node.default_child
        cfn_stage.default_route_settings = {
            "throttlingBurstLimit": 100,
            "throttlingRateLimit": 50,
        }

        self.dashboard_lambda = dashboard_lambda = lambda_.Function(
            self, "DashboardApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/dashboard"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                # New: /table-indexes endpoint runs pg_stat_user_indexes against
                # the live target cluster (not the cache DB), so the handler
                # needs to resolve cluster_arn/secret_arn from the registry.
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        data.cache_db.secret.grant_read(dashboard_lambda)
        data.cache_db.grant_data_api_access(dashboard_lambda)
        foundation.clusters_table.grant_read_data(dashboard_lambda)
        dashboard_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds-data:ExecuteStatement",
                "secretsmanager:GetSecretValue",
            ],
            resources=["*"],
        ))
        # PG Log Insights panel hits CloudWatch Logs Insights directly via
        # start_query / get_query_results. Scoped to /aws/rds/cluster/* so
        # the panel can't read unrelated log groups even if cluster_id is
        # spoofed in the URL.
        dashboard_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "logs:StartQuery",
                "logs:GetQueryResults",
                "logs:StopQuery",
            ],
            resources=[
                "arn:aws:logs:*:*:log-group:/aws/rds/cluster/*",
                "arn:aws:logs:*:*:log-group:/aws/rds/cluster/*:*",
            ],
        ))
        # DescribeLogGroups is unscoped (AWS limitation — Insights queries
        # need it to validate group existence before scanning).
        dashboard_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["logs:DescribeLogGroups"],
            resources=["*"],
        ))
        # Replication Topology panel reads cluster member list + per-instance
        # AuroraReplicaLag from CloudWatch on demand. RDS Describe* APIs
        # don't support resource ARN scoping, so both must be "*".
        dashboard_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
                # Backup inventory panel — read-only snapshot listing.
                "rds:DescribeDBClusterSnapshots",
                "cloudwatch:GetMetricStatistics",
            ],
            resources=["*"],
        ))

        # Simulation API — REST mirror of the Simulation MCP tool surface
        # so the dashboard UI can render "what-if" panels without going
        # through the chat agent.
        simulation_lambda = lambda_.Function(
            self, "SimulationApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/simulation"),
            # describe_db_engine_versions can be slow on first cold start
            # in a new region; 30s allows the call to complete.
            timeout=cdk.Duration.seconds(30),
            memory_size=512,
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(simulation_lambda)
        data.cache_db.grant_data_api_access(simulation_lambda)
        simulation_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds-data:ExecuteStatement",
                "secretsmanager:GetSecretValue",
                "rds:DescribeDBClusters",
                "rds:DescribeDBEngineVersions",
            ],
            resources=["*"],
        ))

        clusters_lambda = lambda_.Function(
            self, "ClustersApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/clusters"),
            # Sample seeder issues ~40 INSERTs over RDS Data API; 30s is too tight.
            timeout=cdk.Duration.seconds(90),
            memory_size=512,
            environment={
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        foundation.clusters_table.grant_read_write_data(clusters_lambda)
        data.cache_db.secret.grant_read(clusters_lambda)
        data.cache_db.grant_data_api_access(clusters_lambda)
        clusters_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["rds:DescribeDBClusters", "sts:AssumeRole"],
            resources=["*"],
        ))
        # Convention-based credential discovery: probe Secrets Manager for
        # `dbops/<cluster_id>/readonly` during bulk Discover. Scoped to the
        # dbops/* name pattern in any region/account so cross-account
        # discovery (via assumed spoke role) still works. Without this, the
        # secret lookup quietly falls back to master.
        clusters_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["secretsmanager:DescribeSecret"],
            resources=["arn:aws:secretsmanager:*:*:secret:dbops/*"],
        ))

        reports_lambda = lambda_.Function(
            self, "ReportsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/reports"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(reports_lambda)
        data.cache_db.grant_data_api_access(reports_lambda)

        approvals_lambda = lambda_.Function(
            self, "ApprovalsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/approvals"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "APPROVALS_TABLE": foundation.approvals_table.table_name,
            },
        )
        foundation.approvals_table.grant_read_write_data(approvals_lambda)

        # Runbooks API — CRUD over the `runbooks` cache table. AI-generated
        # diagnoses can be saved as reusable playbooks for pattern recurrence.
        runbooks_lambda = lambda_.Function(
            self, "RunbooksApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/runbooks"),
            timeout=cdk.Duration.seconds(15),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(runbooks_lambda)
        data.cache_db.grant_data_api_access(runbooks_lambda)
        runbooks_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["rds-data:ExecuteStatement", "secretsmanager:GetSecretValue"],
            resources=["*"],
        ))

        # Backups write API — manual snapshot creation (phase 2). Human-
        # initiated admin write, audit-logged to PG. Separate from the
        # read tier (dashboard /backups) and from the agent approval path.
        backups_lambda = lambda_.Function(
            self, "BackupsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/backups"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(backups_lambda)
        data.cache_db.grant_data_api_access(backups_lambda)
        backups_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                # CreateDBClusterSnapshot is non-destructive (adds a
                # backup). AddTags lets us stamp dbops:created-by.
                "rds:CreateDBClusterSnapshot",
                "rds:AddTagsToResource",
                "rds-data:ExecuteStatement",
                "secretsmanager:GetSecretValue",
            ],
            resources=["*"],
        ))

        # Chat Sessions API — persists chat conversations across devices.
        # Backed by the existing `sessions` DDB table (PK session_id) plus
        # a user-updated GSI for cheap list-by-user.
        chat_sessions_lambda = lambda_.Function(
            self, "ChatSessionsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/chat_sessions"),
            timeout=cdk.Duration.seconds(10),
            environment={
                "SESSIONS_TABLE": foundation.sessions_table.table_name,
            },
        )
        foundation.sessions_table.grant_read_write_data(chat_sessions_lambda)

        # Saved Queries API — durable Query Lab scratchpad
        saved_queries_lambda = lambda_.Function(
            self, "SavedQueriesApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/saved_queries"),
            timeout=cdk.Duration.seconds(10),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(saved_queries_lambda)
        data.cache_db.grant_data_api_access(saved_queries_lambda)
        saved_queries_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["rds-data:ExecuteStatement", "secretsmanager:GetSecretValue"],
            resources=["*"],
        ))

        # Agent Memory API — read + delete user's AgentCore Memory records
        # so the DBA can see (and prune) what the agent has remembered.
        memory_lambda = lambda_.Function(
            self, "MemoryApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/memory"),
            timeout=cdk.Duration.seconds(10),
            environment={
                "MEMORY_ID": self.memory.memory_id,
            },
        )
        memory_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:ListMemoryRecords",
                "bedrock-agentcore:GetMemoryRecord",
                "bedrock-agentcore:DeleteMemoryRecord",
            ],
            # Scope to this memory resource specifically — no chance of
            # the API leaking into another memory store in the account.
            resources=[self.memory.memory_arn],
        ))

        self.alerts_lambda = alerts_lambda = lambda_.Function(
            self, "AlertsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/alerts"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "ALERT_TOPIC_ARN": data.alert_topic.topic_arn,
            },
        )
        data.cache_db.secret.grant_read(alerts_lambda)
        data.cache_db.grant_data_api_access(alerts_lambda)
        data.alert_topic.grant_publish(alerts_lambda)
        alerts_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["sns:Subscribe", "sns:Unsubscribe", "sns:ListSubscriptionsByTopic"],
            resources=[data.alert_topic.topic_arn, "*"],
        ))

        # Slack interactive endpoint — verifies HMAC signature and acks
        # alerts in-place. Disabled when SLACK_SIGNING_SECRET is empty:
        # the env var is still set, the handler returns a self-explaining
        # 200 ephemeral message so the user can fix configuration without
        # hunting through CloudWatch.
        slack_interactive_lambda = lambda_.Function(
            self, "SlackInteractiveApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/slack_interactive"),
            timeout=cdk.Duration.seconds(15),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "SLACK_SIGNING_SECRET": Settings.SLACK_SIGNING_SECRET,
            },
        )

        # Slack slash-command endpoint — `/dbops status|timeline|
        # clusters [args]`. Shares the signing secret with the
        # interactive Lambda so workspaces don't need a second secret.
        slack_command_lambda = lambda_.Function(
            self, "SlackCommandApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/slack_command"),
            timeout=cdk.Duration.seconds(10),
            environment={
                "SLACK_SIGNING_SECRET": Settings.SLACK_SIGNING_SECRET,
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "FRONTEND_URL": Settings.FRONTEND_URL,
            },
        )
        foundation.clusters_table.grant_read_data(slack_command_lambda)

        # Self-health endpoint: aggregates Lambda + Aurora + DDB state
        # so /health renders a single-page operational picture of DBOps
        # itself. Read-only IAM scoped to describe/list calls.
        health_lambda = lambda_.Function(
            self, "HealthApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/health"),
            timeout=cdk.Duration.seconds(15),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "SESSIONS_TABLE": foundation.sessions_table.table_name,
                "APPROVALS_TABLE": foundation.approvals_table.table_name,
            },
        )
        health_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "lambda:ListFunctions",
                "rds:DescribeDBClusters",
                "dynamodb:DescribeTable",
            ],
            resources=["*"],
        ))
        data.cache_db.secret.grant_read(slack_interactive_lambda)
        data.cache_db.grant_data_api_access(slack_interactive_lambda)
        slack_interactive_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds-data:ExecuteStatement",
                "secretsmanager:GetSecretValue",
            ],
            resources=["*"],
        ))

        self.api.add_routes(
            path="/api/dashboard/{cluster_id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/timeseries",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTimeseriesIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/wait-events",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardWaitEventsIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/slow-queries",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSlowQueriesIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/query-detail",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardQueryDetailIntegration", dashboard_lambda),
        )
        # Workload diff — pg_stat_statements snapshot delta between two
        # points in time (new / regressed / improved / disappeared
        # queries). Wired from the timeline for "what changed around
        # this deploy".
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/workload-diff",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardWorkloadDiffIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/vacuum-stats",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardVacuumStatsIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/index-recommendations",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardIndexRecsIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/long-running",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardLongRunningIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/batch-timeseries",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardBatchTsIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/blocking-locks",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardBlockingLocksIntegration", dashboard_lambda),
        )
        # PG log analytics (CW Logs Insights pass-through). Distinct route
        # from /dashboard/{id} catch-all so API Gateway resolves to the same
        # Lambda but the path-suffix routing in handler.py picks it up.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/log-insights",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardLogInsightsIntegration", dashboard_lambda),
        )
        # Capacity forecast (linear regression on metric_snapshots → 30/60/90d
        # projections + days_until_limit).
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/capacity-forecast",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardCapacityForecastIntegration", dashboard_lambda),
        )
        # Redundant / prefix-covered / unused indexes (live pg_index query
        # via Data API). PG-only for v1.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/redundant-indexes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardRedundantIndexesIntegration", dashboard_lambda),
        )
        # Replication topology (writer + readers + per-instance replica lag,
        # live RDS Describe + CloudWatch).
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/topology",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTopologyIntegration", dashboard_lambda),
        )
        # Backup inventory — snapshots + PITR window (read-only tier of
        # the backup workflow). Live RDS Describe calls.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/backups",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardBackupsIntegration", dashboard_lambda),
        )
        # Manual snapshot creation (phase 2 write tier) — admin-gated,
        # routed to the dedicated backups Lambda (not the read-only
        # dashboard handler).
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/snapshot",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("BackupsSnapshotIntegration", backups_lambda),
        )
        # SLO tracker — availability + latency SLI computed from the cache,
        # error budget burn-down, per-day timeline.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/slo",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSloIntegration", dashboard_lambda),
        )
        # Schema lineage / FK graph — live pg_constraint introspection.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/schema-graph",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSchemaGraphIntegration", dashboard_lambda),
        )
        # Simulation API — REST mirror of Simulation MCP tools. All write-
        # like operations are simulations, never DDL execution, so POST is
        # safe without an approval flow.
        sim_integration = integrations.HttpLambdaIntegration(
            "SimulationIntegration", simulation_lambda
        )
        for sim_path, sim_methods in [
            ("/api/simulation/parameter-catalog", [apigwv2.HttpMethod.GET]),
            ("/api/simulation/upgrade-compatibility", [apigwv2.HttpMethod.POST]),
            ("/api/simulation/upgrade-impact", [apigwv2.HttpMethod.POST]),
            ("/api/simulation/upgrade-plan", [apigwv2.HttpMethod.POST]),
            ("/api/simulation/parameter-change", [apigwv2.HttpMethod.POST]),
            ("/api/simulation/scaling", [apigwv2.HttpMethod.POST]),
            ("/api/simulation/ddl-impact", [apigwv2.HttpMethod.POST]),
        ]:
            self.api.add_routes(
                path=sim_path, methods=sim_methods, integration=sim_integration
            )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/settings",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSettingsIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/schema-changes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSchemaChangesIntegration", dashboard_lambda),
        )
        # Unified incident timeline — merges event_log + schema_changes +
        # audit_log into one chronological feed for incident triage.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/timeline",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTimelineIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/anomalies",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardAnomaliesIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/audit-log",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardAuditLogIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/multi-cluster/overview",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("MultiClusterOverviewIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/table-sizes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTableSizesIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/table-indexes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTableIndexesIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/health-findings",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardHealthFindingsIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/extensions",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardExtensionsIntegration", dashboard_lambda),
        )
        # ===== Application Inference Profiles (cost-allocation tagging) =====
        # One AIP per supported Claude generation, all tagged with Application=DBOps
        # so Cost Explorer attributes Bedrock spend to this app.
        aip_setup_lambda = lambda_.Function(
            self, "InferenceProfileSetup",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../data-pipeline/inference_profile_setup"),
            timeout=cdk.Duration.minutes(3),
            memory_size=256,
            environment={
                "ENV": Settings.ENV,
                "ACCOUNT_ID": Settings.ACCOUNT_ID,
            },
        )
        aip_setup_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "bedrock:CreateInferenceProfile",
                "bedrock:DeleteInferenceProfile",
                "bedrock:ListInferenceProfiles",
                "bedrock:GetInferenceProfile",
                "bedrock:TagResource",
                "bedrock:UntagResource",
                "bedrock:ListTagsForResource",
                "ssm:PutParameter",
                "ssm:DeleteParameter",
                "ssm:GetParameter",
            ],
            resources=["*"],
        ))
        from aws_cdk import custom_resources as cr
        aip_provider = cr.Provider(self, "InferenceProfileProvider", on_event_handler=aip_setup_lambda)
        cdk.CustomResource(
            self, "InferenceProfileSetupRun",
            service_token=aip_provider.service_token,
            properties={"version": "v1"},
        )

        # ===== Cost API =====
        cost_lambda = lambda_.Function(
            self, "CostApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/cost"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "ENV": Settings.ENV,
            },
        )
        cost_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "ce:GetCostAndUsage",
                "ce:GetCostAndUsageWithResources",
                "ce:GetTags",
                # Needed to enumerate per-model Bedrock SERVICE entries that
                # AWS adds over time (e.g. "Claude Sonnet 4.6 (Amazon Bedrock
                # Edition)") so the cost filter doesn't go stale.
                "ce:GetDimensionValues",
            ],
            resources=["*"],
        ))
        self.api.add_routes(
            path="/api/cost",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("CostIntegration", cost_lambda),
        )

        models_lambda = lambda_.Function(
            self, "ModelsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/models"),
            # multi-region ListInferenceProfiles can take 8-12s × 4 regions on cold start
            timeout=cdk.Duration.seconds(60),
            memory_size=512,
            environment={
                "DEFAULT_MODEL_ID": Settings.AGENT_MODEL_ID,
            },
        )
        models_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:ListInferenceProfiles", "bedrock:GetInferenceProfile"],
            resources=["*"],
        ))
        self.api.add_routes(
            path="/api/models",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("ModelsIntegration", models_lambda),
        )

        explain_lambda = lambda_.Function(
            self, "ExplainApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/explain"),
            # EXPLAIN ANALYZE on a heavy query can hit minutes; cap to 60s so a
            # runaway plan doesn't pin the visualizer indefinitely.
            timeout=cdk.Duration.seconds(60),
            memory_size=512,
            environment={
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        foundation.clusters_table.grant_read_data(explain_lambda)
        explain_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds-data:ExecuteStatement",
                "secretsmanager:GetSecretValue",
            ],
            resources=["*"],
        ))
        self.api.add_routes(
            path="/api/explain",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("ExplainIntegration", explain_lambda),
        )

        alerts_integration = integrations.HttpLambdaIntegration("AlertsIntegration", alerts_lambda)
        self.api.add_routes(
            path="/api/alert-rules",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=alerts_integration,
        )
        self.api.add_routes(
            path="/api/alert-rules/{id}",
            methods=[apigwv2.HttpMethod.PATCH, apigwv2.HttpMethod.DELETE],
            integration=alerts_integration,
        )
        # /impact: returns the operational context around a rule's
        # most-recent firing (slow queries, concurrent events, sibling
        # alerts). Read-only.
        self.api.add_routes(
            path="/api/alert-rules/{id}/impact",
            methods=[apigwv2.HttpMethod.GET],
            integration=alerts_integration,
        )
        self.api.add_routes(
            path="/api/alert-subscriptions",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
            integration=alerts_integration,
        )
        # Slack interactive ack — the URL paired with the Slack app's
        # "Interactivity Request URL" setting.
        self.api.add_routes(
            path="/api/slack/interactive",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("SlackInteractiveIntegration", slack_interactive_lambda),
        )
        # Slack slash command — URL paired with the Slack app's
        # `/dbops` command settings. Same signing secret as interactive.
        self.api.add_routes(
            path="/api/slack/command",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("SlackCommandIntegration", slack_command_lambda),
        )
        # DBOps self-monitoring health endpoint
        self.api.add_routes(
            path="/api/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("HealthIntegration", health_lambda),
        )
        clusters_integration = integrations.HttpLambdaIntegration("ClustersIntegration", clusters_lambda)
        self.api.add_routes(
            path="/api/clusters",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=clusters_integration,
        )
        # Discovery + bulk register + sample seeder share the same Lambda; handler dispatches on path.
        self.api.add_routes(
            path="/api/clusters/discover",
            methods=[apigwv2.HttpMethod.POST],
            integration=clusters_integration,
        )
        # Pre-flight: AssumeRole + DescribeDBClusters without saving.
        # Lets the DBA verify a spoke role works before committing.
        self.api.add_routes(
            path="/api/clusters/test-connection",
            methods=[apigwv2.HttpMethod.POST],
            integration=clusters_integration,
        )
        self.api.add_routes(
            path="/api/clusters/bulk-register",
            methods=[apigwv2.HttpMethod.POST],
            integration=clusters_integration,
        )
        # Demo mode: synthesises 24h of cache data + a fake cluster row so an
        # evaluator can see every panel before they connect a real cluster.
        self.api.add_routes(
            path="/api/clusters/sample",
            methods=[apigwv2.HttpMethod.POST],
            integration=clusters_integration,
        )
        # DELETE /api/clusters/{id} — removes the DDB row + demo cache rows if is_demo.
        self.api.add_routes(
            path="/api/clusters/{id}",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=clusters_integration,
        )
        self.api.add_routes(
            path="/api/reports",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("ReportsIntegration", reports_lambda),
        )
        self.api.add_routes(
            path="/api/reports/{id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("ReportDetailIntegration", reports_lambda),
        )
        self.api.add_routes(
            path="/api/approvals",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("ApprovalsIntegration", approvals_lambda),
        )
        self.api.add_routes(
            path="/api/approvals/{id}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.PUT],
            integration=integrations.HttpLambdaIntegration("ApprovalDetailIntegration", approvals_lambda),
        )
        # DBOps activity log — chronological feed of every approval
        # (any status) for retro + compliance. Reads the same DDB
        # table; routed through the approvals lambda to avoid wiring
        # a second one.
        self.api.add_routes(
            path="/api/activity",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("ActivityIntegration", approvals_lambda),
        )
        # Runbooks — AI-generated playbooks
        runbooks_integration = integrations.HttpLambdaIntegration(
            "RunbooksIntegration", runbooks_lambda
        )
        self.api.add_routes(
            path="/api/runbooks",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=runbooks_integration,
        )
        self.api.add_routes(
            path="/api/runbooks/{id}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.DELETE],
            integration=runbooks_integration,
        )

        # Chat sessions — cross-device conversation persistence
        chat_sessions_integration = integrations.HttpLambdaIntegration(
            "ChatSessionsIntegration", chat_sessions_lambda
        )
        self.api.add_routes(
            path="/api/chat/sessions",
            methods=[apigwv2.HttpMethod.GET],
            integration=chat_sessions_integration,
        )
        self.api.add_routes(
            path="/api/chat/sessions/{id}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.PUT,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=chat_sessions_integration,
        )

        # Agent memory inspector
        memory_integration = integrations.HttpLambdaIntegration(
            "MemoryIntegration", memory_lambda
        )
        self.api.add_routes(
            path="/api/memory",
            methods=[apigwv2.HttpMethod.GET],
            integration=memory_integration,
        )
        self.api.add_routes(
            path="/api/memory/{id}",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=memory_integration,
        )

        # Saved queries — durable Query Lab scratchpad
        saved_queries_integration = integrations.HttpLambdaIntegration(
            "SavedQueriesIntegration", saved_queries_lambda
        )
        self.api.add_routes(
            path="/api/saved-queries",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=saved_queries_integration,
        )
        self.api.add_routes(
            path="/api/saved-queries/{id}",
            methods=[
                apigwv2.HttpMethod.GET,
                apigwv2.HttpMethod.PUT,
                apigwv2.HttpMethod.DELETE,
            ],
            integration=saved_queries_integration,
        )

        # ===== Outputs =====

        cdk.CfnOutput(self, "ApiUrl", value=self.api.url or "")
        cdk.CfnOutput(self, "GatewayId", value=self.gateway.gateway_id)
        cdk.CfnOutput(self, "RuntimeArn", value=self.runtime.agent_runtime_arn)
        cdk.CfnOutput(self, "MemoryId", value=self.memory.memory_id)
