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
            },
        )
        data.cache_db.secret.grant_read(operations_mcp_lambda)
        data.cache_db.grant_data_api_access(operations_mcp_lambda)
        foundation.clusters_table.grant_read_data(operations_mcp_lambda)
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
        self.api.add_routes(
            path="/api/alert-subscriptions",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
            integration=alerts_integration,
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

        # ===== Outputs =====

        cdk.CfnOutput(self, "ApiUrl", value=self.api.url or "")
        cdk.CfnOutput(self, "GatewayId", value=self.gateway.gateway_id)
        cdk.CfnOutput(self, "RuntimeArn", value=self.runtime.agent_runtime_arn)
        cdk.CfnOutput(self, "MemoryId", value=self.memory.memory_id)
