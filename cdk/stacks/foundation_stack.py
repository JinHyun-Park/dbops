import aws_cdk as cdk
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as apigwv2_integrations,
)
from aws_cdk import (
    aws_cognito as cognito,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from config.settings import Settings
from constructs import Construct


class FoundationStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        self.user_pool = cognito.UserPool(
            self, "UserPool",
            user_pool_name=f"dbops-{Settings.ENV}",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            # cdk-nag AwsSolutions-COG1: enforce a minimum-strength password.
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
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
            # DBA shifts are long; a 1h access token forces too many silent
            # refreshes. Refresh token (30d default) covers idle reopens.
            access_token_validity=cdk.Duration.hours(12),
            id_token_validity=cdk.Duration.hours(12),
        )

        self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=Settings.COGNITO_DOMAIN_PREFIX,
            ),
        )

        # RBAC groups. Default model: every user is admin UNLESS explicitly
        # placed in dbops-viewer. This is the pragmatic stance for an
        # ops-team product — opt-in restriction rather than opt-in privilege.
        cognito.CfnUserPoolGroup(
            self,
            "AdminGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="dbops-admin",
            description="Full read/write access to clusters, alerts, approvals.",
            precedence=1,
        )
        cognito.CfnUserPoolGroup(
            self,
            "ViewerGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="dbops-viewer",
            description="Read-only: can view dashboards and use chat, but cannot register clusters or modify alerts.",
            precedence=10,
        )

        self.clusters_table = dynamodb.Table(
            self, "ClustersTable",
            table_name=f"dbops-{Settings.ENV}-clusters",
            partition_key=dynamodb.Attribute(name="cluster_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        self.sessions_table = dynamodb.Table(
            self, "SessionsTable",
            table_name=f"dbops-{Settings.ENV}-sessions",
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="ttl",
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        # Listing sessions belonging to a specific user is the hot path
        # (sidebar load). The GSI lets us avoid a full table scan: query
        # by user_id, ordered by updated_at DESC.
        self.sessions_table.add_global_secondary_index(
            index_name="user-updated-index",
            partition_key=dynamodb.Attribute(name="user_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="updated_at", type=dynamodb.AttributeType.NUMBER),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        self.approvals_table = dynamodb.Table(
            self, "ApprovalsTable",
            table_name=f"dbops-{Settings.ENV}-approvals",
            partition_key=dynamodb.Attribute(name="approval_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
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

        # ===== In-app alert push (scoped) — WebSocket channel =====
        # Real-time push of fired alerts / external incidents to connected
        # operators so they don't wait for the next dashboard poll. Lives in
        # foundation so data (alert_evaluator), agent (incident_webhook) and
        # frontend (config.json) can all reference it without a cross-stack cycle.
        self.ws_connections_table = dynamodb.Table(
            self, "WsConnections",
            table_name=f"dbops-{Settings.ENV}-ws-connections",
            partition_key=dynamodb.Attribute(name="connection_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        _ws_env = {"WS_CONNECTIONS_TABLE": self.ws_connections_table.table_name}
        ws_connect_fn = lambda_.Function(
            self, "WsConnect",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/ws_connect"),
            timeout=cdk.Duration.seconds(10),
            environment=_ws_env,
        )
        ws_disconnect_fn = lambda_.Function(
            self, "WsDisconnect",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/ws_disconnect"),
            timeout=cdk.Duration.seconds(10),
            environment=_ws_env,
        )
        # Authorizer validates the Cognito ACCESS token (passed as ?token=) via
        # Cognito GetUser — no IAM, no JWKS/crypto bundling.
        ws_authorizer_fn = lambda_.Function(
            self, "WsAuthorizer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/ws_authorizer"),
            timeout=cdk.Duration.seconds(10),
        )
        self.ws_connections_table.grant_read_write_data(ws_connect_fn)
        self.ws_connections_table.grant_read_write_data(ws_disconnect_fn)

        self.ws_api = apigwv2.WebSocketApi(
            self, "AlertWs",
            api_name=f"dbops-{Settings.ENV}-alert-ws",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=apigwv2_integrations.WebSocketLambdaIntegration(
                    "WsConnectInt", ws_connect_fn,
                ),
                authorizer=apigwv2_authorizers.WebSocketLambdaAuthorizer(
                    "WsAuth", ws_authorizer_fn,
                    # The Cognito access token rides the query string (browsers
                    # can't set WS headers). Acceptable only because: wss:// is
                    # TLS so the query string is inside the encrypted GET (proxies
                    # see only CONNECT host:443), AND this stage has NO access
                    # logging (see the stage below). HARDENING GUARD: if you ever
                    # need WS access logs, FIRST switch to the WS-ticket pattern
                    # (short-lived single-use nonce in the URL instead of the
                    # token) — see BACKLOG "WS-ticket". Otherwise the token lands
                    # in CloudWatch in plaintext.
                    identity_source=["route.request.querystring.token"],
                ),
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=apigwv2_integrations.WebSocketLambdaIntegration(
                    "WsDisconnectInt", ws_disconnect_fn,
                ),
            ),
        )
        # HARDENING GUARD: do NOT add access_log_settings here without first
        # implementing the WS-ticket pattern (BACKLOG "WS-ticket"). The connect
        # authorizer's identity source is the access token in the query string;
        # WS access logs would record it in plaintext. No access logging today =
        # the token-in-URL exposure is mitigated.
        self.ws_stage = apigwv2.WebSocketStage(
            self, "AlertWsStage",
            web_socket_api=self.ws_api,
            stage_name="prod",
            auto_deploy=True,
        )
        # wss:// connect URL (frontend) + https:// management endpoint (broadcasters).
        self.ws_mgmt_endpoint = self.ws_stage.callback_url

        cdk.CfnOutput(self, "AlertWsUrl", value=self.ws_stage.url)
        cdk.CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        cdk.CfnOutput(self, "UserPoolClientId", value=self.user_pool_client.user_pool_client_id)

    def grant_alert_broadcast(self, fn) -> None:
        """Wire a Lambda to broadcast over the WS alert channel (env + grants).
        Used by data (alert_evaluator) and agent (incident_webhook)."""
        fn.add_environment("WS_CONNECTIONS_TABLE", self.ws_connections_table.table_name)
        fn.add_environment("WS_MGMT_ENDPOINT", self.ws_mgmt_endpoint)
        self.ws_connections_table.grant_read_write_data(fn)
        self.ws_api.grant_manage_connections(fn)
