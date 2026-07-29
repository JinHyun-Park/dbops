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

        # A Cognito Hosted UI prefix is GLOBALLY UNIQUE PER REGION. Shipping a
        # literal in settings.example.py meant every newcomer deploying to the
        # example's own default region collided with the first deployment and the
        # FIRST stack in the dependency chain rolled back with "domain already
        # exists", so nothing downstream ever deployed. Empty setting = derive a
        # prefix that is unique by construction. An explicit value still wins, so
        # existing deployments keep the domain their Hosted UI URL already uses.
        # Exported (not just local) because frontend_stack builds the Hosted-UI
        # URL in /config.json from it. Reading Settings.COGNITO_DOMAIN_PREFIX
        # there instead shipped "https://.auth.<region>.amazoncognito.com"
        # whenever the setting was left empty (the documented fresh-account
        # default): an unresolvable host, so nobody could log in while every
        # stack deployed green.
        self.cognito_domain_prefix = (Settings.COGNITO_DOMAIN_PREFIX or "").strip()
        if not self.cognito_domain_prefix:
            self.cognito_domain_prefix = f"dbops-{Settings.ENV}-{str(Settings.ACCOUNT_ID)[-6:]}"
        self.user_pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=self.cognito_domain_prefix,
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

        # ===== Multi-team tenancy =====
        # teams: one row per team. team_members: one row per (team, member);
        # the by-user GSI answers "which teams is this user in?" in O(1) for the
        # per-request visibility overlay (api/*/tenancy.py).
        self.teams_table = dynamodb.Table(
            self, "TeamsTable",
            table_name=f"dbops-{Settings.ENV}-teams",
            partition_key=dynamodb.Attribute(name="team_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.team_members_table = dynamodb.Table(
            self, "TeamMembersTable",
            table_name=f"dbops-{Settings.ENV}-team-members",
            partition_key=dynamodb.Attribute(name="team_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="username", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        self.team_members_table.add_global_secondary_index(
            index_name="by-user",
            partition_key=dynamodb.Attribute(name="username", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
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

        # ===== Agent Tasks — event-driven & scheduled agent work =====
        # Records of autonomous agent work: auto-RCA on alert, scheduled
        # reports, manual runs. Lives in foundation so data (alert_evaluator,
        # task_scheduler) can ENQUEUE and agent (task_worker, tasks API) can
        # PROCESS without a cross-stack cycle. The table's STREAM is the single
        # processing trigger — any pending row, from any source, drives the
        # worker. See docs/superpowers/specs/2026-06-18-agent-tasks-design.md.
        self.agent_tasks_table = dynamodb.Table(
            self, "AgentTasksTable",
            table_name=f"dbops-{Settings.ENV}-agent-tasks",
            partition_key=dynamodb.Attribute(name="task_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            stream=dynamodb.StreamViewType.NEW_IMAGE,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        # Per-cluster recent tasks: dashboard cluster view + alert_evaluator
        # dedupe (skip a fresh auto-RCA if one already ran minutes ago).
        self.agent_tasks_table.add_global_secondary_index(
            index_name="cluster-created-index",
            partition_key=dynamodb.Attribute(name="cluster_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        # Fleet-wide recent tasks (the /tasks list). Constant partition key
        # (record_type == "task") so we Query by recency instead of scanning.
        self.agent_tasks_table.add_global_secondary_index(
            index_name="recency-index",
            partition_key=dynamodb.Attribute(name="record_type", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # ===== App Config — in-app, DB-backed feature toggles =====
        # Small key-value store an ADMIN edits from the web UI (GET/PUT
        # /api/config) to flip opt-in features (ticketing provider, report
        # delivery) WITHOUT a redeploy. Lives in foundation so the agent stack
        # (config API + task worker) and the data stack (report generator) can
        # all reach it without a cross-stack cycle — same rationale as the
        # agent_tasks_table above. Read precedence at consumers is
        # DB value -> env var -> default, so a fresh deploy with no rows here
        # behaves exactly as the baked-in env defaults.
        self.app_config_table = dynamodb.Table(
            self, "AppConfigTable",
            table_name=f"dbops-{Settings.ENV}-app-config",
            partition_key=dynamodb.Attribute(name="config_key", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ===== Approval Policies — designated-approver routing =====
        # Admin-defined policies that restrict WHO may approve specific
        # cluster/action requests (advanced approval). Lives in foundation so
        # the policy CRUD API and the approvals API (both agent stack) reach it
        # via grant helpers without a cross-stack cycle. A request with no
        # matching policy falls back to the existing "any admin" rule, so an
        # empty table reproduces today's behavior.
        self.approval_policies_table = dynamodb.Table(
            self, "ApprovalPoliciesTable",
            table_name=f"dbops-{Settings.ENV}-approval-policies",
            partition_key=dynamodb.Attribute(name="policy_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,  # cdk-nag AwsSolutions-DDB3
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ===== Context Files — operator-uploaded reference context =====
        # Small text files (org charts, tagging conventions, account↔owner
        # mappings) an ADMIN uploads; their text is injected into the agent's
        # system prompt as fenced operator-provided reference DATA (not
        # commands). Global / platform-wide. Lives in foundation so the agent
        # Runtime (agent stack) + the CRUD API can both reach it.
        self.context_files_table = dynamodb.Table(
            self, "ContextFilesTable",
            table_name=f"dbops-{Settings.ENV}-context-files",
            partition_key=dynamodb.Attribute(name="file_id", type=dynamodb.AttributeType.STRING),
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

        # Short-lived single-use handshake tickets (WS-ticket pattern). The
        # `ttl` attribute only REAPS rows; the authorizer checks `expires_at`
        # against its own clock, because DynamoDB TTL deletion is best-effort and
        # can lag by up to 48 hours. Treating TTL as the expiry would turn a
        # 60-second ticket into a 48-hour one.
        self.ws_tickets_table = dynamodb.Table(
            self, "WsTicketsTable",
            table_name=f"dbops-{Settings.ENV}-ws-tickets",
            partition_key=dynamodb.Attribute(name="ticket", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            # cdk-nag AwsSolutions-DDB3, same as the connections table. Pointless
            # operationally on rows that live 60 seconds, but the nag gate is the
            # CI gate and a suppression costs more to justify than the setting
            # costs on a table this small.
            point_in_time_recovery=True,
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
        # Authorizer consumes a single-use TICKET (passed as ?ticket=) minted by
        # POST /api/ws-ticket. It replaced Cognito-GetUser-on-an-access-token so
        # that no long-lived credential rides the query string; see
        # api/ws_authorizer/handler.py for why the ticket, not the DynamoDB TTL,
        # is what enforces expiry.
        ws_authorizer_fn = lambda_.Function(
            self, "WsAuthorizer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/ws_authorizer"),
            timeout=cdk.Duration.seconds(10),
            environment={"WS_TICKETS_TABLE": self.ws_tickets_table.table_name},
        )
        # READ+WRITE, not read: the authorizer CONSUMES the ticket with a
        # conditional delete, which is what makes it single-use.
        self.ws_tickets_table.grant_read_write_data(ws_authorizer_fn)
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
                    # A single-use TICKET rides the query string, not the Cognito
                    # access token. Browsers still can't set WS headers, so
                    # something has to be in the URL; the point is that this value
                    # is random, lives 60 seconds, is consumed by the first
                    # handshake that presents it, and authorizes nothing else.
                    # The real credential stays in the Authorization header of the
                    # ordinary REST call that mints it (POST /api/ws-ticket, behind
                    # the API's JWT authorizer).
                    identity_source=["route.request.querystring.ticket"],
                ),
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=apigwv2_integrations.WebSocketLambdaIntegration(
                    "WsDisconnectInt", ws_disconnect_fn,
                ),
            ),
        )
        # Access logging is now SAFE to add here, and that is the whole point of
        # the WS-ticket work: the identity source in the query string is a
        # single-use 60-second ticket, so a log line recording it captures
        # something already spent and worth nothing. It stays off only because
        # nothing needs it yet, not because enabling it would leak a credential.
        # (Before the ticket, the access token was in the URL and logs would have
        # written it to CloudWatch in plaintext.)
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

    def grant_task_enqueue(self, fn) -> None:
        """Wire a Lambda to ENQUEUE agent tasks: write pending rows and query
        the per-cluster GSI for dedupe. Used by data (alert_evaluator,
        task_scheduler). grant_read_write_data covers the table + its indexes."""
        fn.add_environment("AGENT_TASKS_TABLE", self.agent_tasks_table.table_name)
        self.agent_tasks_table.grant_read_write_data(fn)

    def grant_task_manage(self, fn) -> None:
        """Wire a Lambda to PROCESS/read agent tasks (the stream worker and the
        list/get API). Includes table + index read/write."""
        fn.add_environment("AGENT_TASKS_TABLE", self.agent_tasks_table.table_name)
        self.agent_tasks_table.grant_read_write_data(fn)

    def grant_ws_ticket_mint(self, fn) -> None:
        """Wire a Lambda to MINT WebSocket handshake tickets (env + write grant).

        Write-only on purpose: the minting endpoint has no reason to read or delete
        a ticket, and the authorizer is the only thing that consumes one. Keeping
        the two grants disjoint means a bug in the API cannot spend a ticket.
        """
        fn.add_environment("WS_TICKETS_TABLE", self.ws_tickets_table.table_name)
        self.ws_tickets_table.grant_write_data(fn)

    def grant_app_config_read(self, fn) -> None:
        """Wire a Lambda to READ the app-config table (env + read grant).
        Used by feature consumers (task_worker via ticketing, report_generator)
        to resolve DB-backed toggles with an env/default fallback."""
        fn.add_environment("APP_CONFIG_TABLE", self.app_config_table.table_name)
        self.app_config_table.grant_read_data(fn)

    def grant_app_config_write(self, fn) -> None:
        """Wire a Lambda to READ/WRITE the app-config table (env + R/W grant).
        Used by the config API (GET/PUT /api/config), admin-gated in the handler."""
        fn.add_environment("APP_CONFIG_TABLE", self.app_config_table.table_name)
        self.app_config_table.grant_read_write_data(fn)

    def grant_approval_policy_read(self, fn) -> None:
        """Wire a Lambda to READ approval policies (env + read grant). Used by
        the approvals API to resolve a request's eligible approver set."""
        fn.add_environment("APPROVAL_POLICIES_TABLE", self.approval_policies_table.table_name)
        self.approval_policies_table.grant_read_data(fn)

    def grant_approval_policy_write(self, fn) -> None:
        """Wire a Lambda to READ/WRITE approval policies (env + R/W grant). Used
        by the policy CRUD API, admin-gated in the handler."""
        fn.add_environment("APPROVAL_POLICIES_TABLE", self.approval_policies_table.table_name)
        self.approval_policies_table.grant_read_write_data(fn)

    def grant_context_files_read(self, fn) -> None:
        """Wire a Lambda to READ context files (env + read grant). Used by the
        agent Runtime to inject operator reference context into the prompt."""
        fn.add_environment("CONTEXT_FILES_TABLE", self.context_files_table.table_name)
        self.context_files_table.grant_read_data(fn)

    def grant_context_files_write(self, fn) -> None:
        """Wire a Lambda to READ/WRITE context files (env + R/W grant). Used by
        the admin CRUD API."""
        fn.add_environment("CONTEXT_FILES_TABLE", self.context_files_table.table_name)
        self.context_files_table.grant_read_write_data(fn)
