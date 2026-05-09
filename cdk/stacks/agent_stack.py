import aws_cdk as cdk
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_iam as iam,
    aws_lambda as lambda_,
)
import aws_cdk.aws_bedrock_agentcore_alpha as agentcore
from aws_cdk import aws_bedrockagentcore as agentcore_cfn
from constructs import Construct
from config.settings import Settings
from tool_definitions import performance_schema, incident_schema, operations_schema, simulation_schema


class AgentStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, data, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ===== MCP Server Lambdas =====

        perf_mcp_lambda = lambda_.Function(
            self, "PerformanceMCP",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers/mcp_servers/performance"),
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
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers/mcp_servers/incident"),
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
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers/mcp_servers/operations"),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            vpc=data.vpc,
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(operations_mcp_lambda)
        data.cache_db.grant_data_api_access(operations_mcp_lambda)

        simulation_mcp_lambda = lambda_.Function(
            self, "SimulationMCP",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers/mcp_servers/simulation"),
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
                allow_headers=["*"],
            ),
        )

        dashboard_lambda = lambda_.Function(
            self, "DashboardApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/dashboard"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
            },
        )
        data.cache_db.secret.grant_read(dashboard_lambda)
        data.cache_db.grant_data_api_access(dashboard_lambda)

        clusters_lambda = lambda_.Function(
            self, "ClustersApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/clusters"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        foundation.clusters_table.grant_read_write_data(clusters_lambda)

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

        self.api.add_routes(
            path="/api/dashboard/{cluster_id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardIntegration", dashboard_lambda),
        )
        self.api.add_routes(
            path="/api/clusters",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("ClustersIntegration", clusters_lambda),
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
