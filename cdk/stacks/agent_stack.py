import aws_cdk as cdk
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrations,
    aws_lambda as lambda_,
)
from constructs import Construct
from config.settings import Settings


class AgentStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, data, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

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

        self.api = apigwv2.HttpApi(
            self, "HttpApi",
            api_name=f"dbops-{Settings.ENV}-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_headers=["*"],
            ),
        )

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

        self.perf_mcp_lambda = perf_mcp_lambda

        cdk.CfnOutput(self, "ApiUrl", value=self.api.url or "")
        cdk.CfnOutput(self, "PerfMcpLambdaArn", value=perf_mcp_lambda.function_arn)
