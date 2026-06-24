import aws_cdk as cdk
import aws_cdk.aws_bedrock_agentcore_alpha as agentcore
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_authorizers as apigwv2_authorizers,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as integrations,
)
from aws_cdk import aws_bedrockagentcore as agentcore_cfn
from aws_cdk import (
    aws_ec2 as ec2,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_lambda_event_sources as lambda_event_sources,
)
from bundling import _PipLocalBundling
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
                # search_logs resolves the cluster's region/spoke_role_arn here
                # so it queries the log group in the cluster's OWN account.
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        data.cache_db.secret.grant_read(incident_mcp_lambda)
        data.cache_db.grant_data_api_access(incident_mcp_lambda)
        foundation.clusters_table.grant_read_data(incident_mcp_lambda)
        # search_logs hits CloudWatch Logs Insights, cross-account-aware. Scoped
        # to /aws/rds/cluster/* so a spoofed cluster_id can't read other groups.
        # sts:AssumeRole lets it hop to the spoke account (local when no role).
        incident_mcp_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "logs:StartQuery",
                "logs:GetQueryResults",
                "logs:StopQuery",
                "logs:DescribeLogGroups",
                "sts:AssumeRole",
            ],
            resources=["*"],
        ))

        # ===== Agent Tasks worker — stream-driven task executor =====
        # Runs the deterministic RCA (and, later, scheduled reports) for tasks
        # enqueued by alert_evaluator / the scheduler / the manual API. Shares
        # the MCP asset so it imports diagnose_root_cause + CacheClient exactly
        # like the incident server. NO VPC: it only touches public AWS endpoints
        # (RDS Data API for the cache, DynamoDB, Secrets Manager, execute-api for
        # the WS push) — same as alert_evaluator, which reads the cache + pushes
        # WS without a VPC. The agent-tasks STREAM is the single trigger, so
        # data-stack enqueuers never invoke this Lambda directly.
        task_worker = lambda_.Function(
            self, "TaskWorker",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="mcp_servers.workers.task_worker.lambda_handler",
            code=lambda_.Code.from_asset("../mcp-servers"),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                # Hybrid RCA: a single Bedrock call turns the deterministic
                # ranked signals into a Korean narrative + recommendations.
                "RCA_NARRATIVE_MODEL_ID": Settings.AGENT_MODEL_ID,
                # Ticketing seam: "none" (default) keeps it inert. Flip via
                # settings once a provider integration ships; an unwired name
                # makes the worker's ticketing step fail loudly.
                "TICKETING_PROVIDER": getattr(Settings, "TICKETING_PROVIDER", "none"),
            },
        )
        data.cache_db.secret.grant_read(task_worker)
        data.cache_db.grant_data_api_access(task_worker)
        foundation.clusters_table.grant_read_data(task_worker)
        foundation.grant_task_manage(task_worker)      # agent-tasks R/W + env
        foundation.grant_app_config_read(task_worker)  # DB-backed TICKETING_PROVIDER
        foundation.grant_alert_broadcast(task_worker)   # WS push on completion
        # Bedrock for the hybrid narrative. The model id is an APAC inference
        # profile, which fans out to foundation models across regions — so the
        # grant must cover both the profile ARNs and the underlying FM ARNs.
        task_worker.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/*",
                f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
            ],
        ))
        # The table stream is the single trigger. Filter to INSERTs so the
        # worker's own running/done UPDATEs don't re-invoke it.
        task_worker.add_event_source(lambda_event_sources.DynamoEventSource(
            foundation.agent_tasks_table,
            starting_position=lambda_.StartingPosition.LATEST,
            batch_size=5,
            retry_attempts=2,
            filters=[lambda_.FilterCriteria.filter(
                {"eventName": lambda_.FilterRule.is_equal("INSERT")}
            )],
        ))

        # Dedicated SG for the operations MCP Lambda. allow_all_outbound=True so
        # the Lambda can reach in-VPC DB native protocols: DocDB 27017 (Mongo
        # wire), ElastiCache 6379 (Redis/Valkey), and Memcached 11211. Mirrors
        # the DocDBMongoCollectorSG pattern in data_stack.py.
        ops_mcp_sg = ec2.SecurityGroup(
            self, "OperationsMcpSG",
            vpc=data.vpc,
            description="operations MCP - egress to in-VPC DB native protocols (DocDB 27017, ElastiCache 6379/11211)",
            allow_all_outbound=True,
        )

        operations_mcp_lambda = lambda_.Function(
            self, "OperationsMCP",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="mcp_servers.operations.handler.lambda_handler",
            # The operations server's DocDB write tools (set_docdb_profiler,
            # create_docdb_index) connect over the Mongo wire protocol, so the
            # asset must carry pymongo (not in the Lambda runtime) + the RDS/DocDB
            # CA bundle. Reuse the collector's Docker-free local-pip bundling
            # (Docker fallback). The other MCP servers share this asset and get
            # pymongo too (harmless, unused).
            code=lambda_.Code.from_asset(
                "../mcp-servers",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    local=_PipLocalBundling("../mcp-servers"),
                    command=[
                        "bash", "-c",
                        "pip install -r requirements.txt -t /asset-output "
                        "&& cp -au . /asset-output "
                        "&& (curl -fsSL -o /asset-output/global-bundle.pem "
                        "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem "
                        "|| true)",
                    ],
                ),
            ),
            timeout=cdk.Duration.minutes(2),
            memory_size=512,
            vpc=data.vpc,
            security_groups=[ops_mcp_sg],
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
        # read_write (not read): the restore_cluster tool registers the new
        # restored cluster row (pending_instance) for the finalizer to pick up.
        foundation.clusters_table.grant_read_write_data(operations_mcp_lambda)
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
                # create_snapshot + restore_cluster (both approval-gated in tool
                # code). Restore stands up a NEW cluster; the source is untouched.
                "rds:CreateDBClusterSnapshot",
                "rds:RestoreDBClusterFromSnapshot",
                "rds:RestoreDBClusterToPointInTime",
                "rds:AddTagsToResource",
                # NoSQL write/remediation (multi-engine #P3.6 Group C). The 3
                # DynamoDB write tools (capacity/TTL/PITR) need Update* + the
                # paired Describe* for the request-time + TOCTOU re-read. All
                # approval-gated in tool code; cross-account via assume-role.
                "dynamodb:UpdateTable",
                "dynamodb:UpdateTimeToLive",
                "dynamodb:UpdateContinuousBackups",
                "dynamodb:DescribeTable",
                "dynamodb:DescribeContinuousBackups",
                "dynamodb:DescribeTimeToLive",
                # Hub-spoke role chaining: control-plane write tools assume the
                # cluster's spoke_role_arn (from the registry) so they target
                # the right account+region instead of the hub.
                "sts:AssumeRole",
            ],
            resources=["*"],
        ))
        # ElastiCache live deep-read (EC-3): describe replication groups and
        # cache clusters to resolve endpoint + engine metadata before connecting.
        # secretsmanager:GetSecretValue and sts:AssumeRole (for cross-account)
        # are already granted above.
        operations_mcp_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticache:DescribeReplicationGroups",
                "elasticache:DescribeCacheClusters",
            ],
            resources=["*"],
        ))
        # ElastiCache write tools (EC-4): modify, snapshot, reboot, failover.
        # All approval-gated via Cedar policy engine.
        operations_mcp_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticache:ModifyReplicationGroup",
                "elasticache:CreateSnapshot",
                "elasticache:RebootCacheCluster",
                "elasticache:TestFailover",
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
                # Simulation tools resolve a cluster's region/spoke_role_arn from
                # the registry (cluster_targets) to make read-only RDS Describe
                # calls in the cluster's own account when grounding simulations
                # in real config (scaling ACU, parameter values, version, members).
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        data.cache_db.secret.grant_read(simulation_mcp_lambda)
        data.cache_db.grant_data_api_access(simulation_mcp_lambda)
        foundation.clusters_table.grant_read_data(simulation_mcp_lambda)
        simulation_mcp_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                # All READ-ONLY: simulations describe real config, never mutate.
                # (Also fixes describe_db_engine_versions for upgrade_compatibility,
                # which previously had no rds permission at all.)
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
                "rds:DescribeDBEngineVersions",
                "rds:DescribeDBClusterParameters",
                "rds:DescribeDBClusterParameterGroups",
                # Real region/edition/instance pricing for scaling cost sims
                # (replaces the hardcoded ACU rate). Price List API is read-only.
                "pricing:GetProducts",
                # Observed Serverless v2 ACU draw for the scaling cost sim
                # (ServerlessDatabaseCapacity) — replaces the min/max midpoint.
                "cloudwatch:GetMetricStatistics",
                # Hub-spoke: assume the cluster's spoke role to describe it in its
                # own account+region (same pattern as the operations write tools).
                "sts:AssumeRole",
                # ElastiCache node-resize cost sim (EC-5): resolve current node
                # type + count before calling the Price List API. Read-only.
                "elasticache:DescribeReplicationGroups",
                "elasticache:DescribeCacheClusters",
            ],
            resources=["*"],
        ))

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

        # ===== Cedar Authorization Policies (Gateway-level) =====
        # The Gateway evaluates these Cedar policies on every tool call: reads
        # auto-allowed, writes need approved==true, DROP/TRUNCATE forbidden
        # without force. They used to be applied MANUALLY via
        # `agentcore policy create` — so on a fresh `cdk deploy` they were never
        # applied and Cedar was DARK (the tool-level approval_guard was the only
        # active control). Wire them into the deploy: one PolicyEngine + one
        # Policy per .cedar file, bound to the Gateway.
        #
        # ROLLOUT MODE = LOG_ONLY: Cedar evaluates and logs every decision to
        # CloudWatch but never DENIES. This lets these never-live-validated
        # policies be observed against real traffic without a default-DENY that
        # would break legitimate reads if the policy schema doesn't match what
        # AgentCore actually passes. Flip to "ENFORCE" only after the decision
        # logs confirm reads permitted + unapproved writes denied. Writes stay
        # protected meanwhile by the tool-level approval_guard.
        import pathlib
        import re as _re

        cedar_mode = "LOG_ONLY"
        cedar_dir = pathlib.Path(__file__).resolve().parent.parent / "policies" / "cedar"

        self.policy_engine = agentcore_cfn.CfnPolicyEngine(
            self, "PolicyEngine",
            name=f"dbops_{Settings.ENV}_policy_engine",
            description="Cedar authorization for the DBOps MCP gateway",
        )

        for _key, _fname in (
            ("performance", "performance_policy.cedar"),
            ("incident", "incident_policy.cedar"),
            ("simulation", "simulation_policy.cedar"),
            ("operations", "operations_policy.cedar"),
        ):
            # AgentCore CreatePolicy accepts exactly ONE Cedar statement per
            # policy — a multi-statement file fails with "unexpected token
            # forbid". Strip // comments FIRST (they can contain ';', e.g.
            # "(no DB change);", which would otherwise corrupt the split), then
            # split the file into individual statements and create one Policy
            # per statement. The .cedar source files stay multi-statement for
            # readability; the split happens only at synth.
            _raw = (cedar_dir / _fname).read_text()
            _statements = [
                s.strip()
                for s in _re.sub(r"//[^\n]*", "", _raw).split(";")
                if s.strip()
            ]
            for _i, _stmt in enumerate(_statements):
                # Action identifiers are scoped to the gateway TARGET (the MCP
                # server). The .cedar source uses a __TARGET__ placeholder;
                # substitute the env-specific target name (matches the
                # CfnGatewayTarget name `dbops-{ENV}-{server}-target` above).
                _stmt = _stmt.replace(
                    "__TARGET__", f"dbops-{Settings.ENV}-{_key}-target"
                )
                # AgentCore requires tool-specific (constrained-action) policies
                # to bind the resource to a SPECIFIC gateway, not just the type
                # ("a constrained action scope was encountered, please constrain
                # the resource to a specific AgentCore::Gateway resource"). The
                # .cedar source uses the readable type form
                # `resource is AgentCore::Gateway`; rewrite it to the concrete
                # gateway entity here (its ARN is only known at deploy).
                _stmt = _stmt.replace(
                    "resource is AgentCore::Gateway",
                    f'resource == AgentCore::Gateway::"{self.gateway.gateway_arn}"',
                )
                agentcore_cfn.CfnPolicy(
                    self, f"CedarPolicy{_key.title()}{_i}",
                    policy_engine_id=self.policy_engine.attr_policy_engine_id,
                    # Name must match ^[A-Za-z][A-Za-z0-9_]*$ — underscores only,
                    # NO hyphens (CFN property validation rejects hyphens).
                    name=f"dbops_{Settings.ENV}_{_key}_{_i}",
                    definition=agentcore_cfn.CfnPolicy.PolicyDefinitionProperty(
                        cedar=agentcore_cfn.CfnPolicy.CedarPolicyProperty(
                            statement=_stmt + ";",
                        ),
                    ),
                    # IGNORE_ALL_FINDINGS during the LOG_ONLY rollout: these
                    # statements aren't yet validated against the live AgentCore
                    # Cedar schema (action / context attribute names), so
                    # FAIL_ON_ANY_FINDINGS could block creation — and we WANT
                    # them CREATED so their decisions are observable in
                    # CloudWatch. Tighten to FAIL_ON_ANY_FINDINGS when flipping
                    # to ENFORCE (after the logs confirm they match).
                    validation_mode="IGNORE_ALL_FINDINGS",
                )

        # Bind the engine to the Gateway. The installed CfnGateway L1 predates
        # the PolicyEngineConfiguration property (the service added it 2026-03),
        # so set it via the escape hatch — the service schema in this region
        # accepts it (get-gateway returns a policyEngineConfiguration field).
        cfn_gateway = self.gateway.node.default_child
        cfn_gateway.add_property_override(
            "PolicyEngineConfiguration",
            {
                "Arn": self.policy_engine.attr_policy_engine_arn,
                "Mode": cedar_mode,
            },
        )
        # The Gateway role must be able to evaluate Cedar against the engine.
        # Verified empirically from the binding's own preflight checks:
        #   - GetPolicyEngine   → on the policy-engine ARN (the engine lookup)
        #   - AuthorizeAction / PartiallyAuthorizeActions → on the GATEWAY ARN
        #     (the gateway's "GenesisPolicyEngineCheck" authorizes against the
        #     gateway resource, not the engine).
        # Grant all three on both ARNs to cover the binding + runtime eval.
        self.gateway.role.add_to_principal_policy(iam.PolicyStatement(
            actions=[
                "bedrock-agentcore:GetPolicyEngine",
                "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions",
            ],
            resources=[
                self.policy_engine.attr_policy_engine_arn,
                # Construct the gateway ARN from pseudo-params + a wildcard for
                # the generated id suffix, rather than referencing
                # self.gateway.gateway_arn. A token reference would make THIS
                # policy depend on the Gateway, while the Gateway also depends on
                # this policy (the binding-ordering dependency above) — a
                # circular dependency. The wildcard matches the real gateway id
                # (e.g. dbops-dev-gateway-jrndurhosm).
                f"arn:aws:bedrock-agentcore:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}"
                f":gateway/dbops-{Settings.ENV}-gateway-*",
            ],
        ))
        # The Gateway's PolicyEngineConfiguration update calls GetPolicyEngine
        # using the Gateway role, so it must run AFTER the role's policy (with
        # that permission) is in place. CFN doesn't infer this ordering — make
        # it explicit (same pattern as the gateway targets above), else the
        # binding fails with "Access denied while calling GetPolicyEngine".
        _gw_default_policy = self.gateway.role.node.try_find_child("DefaultPolicy")
        if _gw_default_policy is not None:
            cfn_gateway.add_dependency(_gw_default_policy.node.default_child)

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
                # AgentCore Runtime REJECTS artifacts containing Python bytecode
                # caches ("artifact contains Python cache files"). Exclude them
                # so a local `python`/py_compile run inside agent/ can't poison
                # the next deploy.
                exclude=["**/__pycache__", "**/*.pyc", "**/*.pyo"],
            ),
            environment_variables={
                "AGENT_MODEL_ID": Settings.AGENT_MODEL_ID,
                "AWS_REGION_OVERRIDE": Settings.REGION,
                "GATEWAY_MCP_URL": gateway_mcp_url,
                # Outbound auth to the Gateway (OAuth2 client-credentials). The
                # default Cognito M2M client the Gateway construct created for us
                # — WITHOUT these the agent's get_gateway_token() returns None,
                # make_mcp_client() returns None, and ZERO of the 42 MCP tools
                # load: the agent could only reach the AWS doc tools, so every DB
                # capability (diagnose_root_cause, execute_sql, query_metrics, …)
                # failed with "tool not found in registry".
                "GATEWAY_TOKEN_URL": self.gateway.token_endpoint_url,
                "GATEWAY_CLIENT_ID": self.gateway.user_pool_client.user_pool_client_id,
                "GATEWAY_CLIENT_SECRET": self.gateway.user_pool_client.user_pool_client_secret.unsafe_unwrap(),
                "GATEWAY_SCOPE": cdk.Fn.join(" ", self.gateway.oauth_scopes),
                # AWS MCP Server (managed, SigV4) — official AWS/Aurora docs.
                # The runtime signs requests with its own IAM role; doc reads
                # need no extra IAM action. getattr defaults keep synth working
                # if a local settings.py predates these keys. Empty disables.
                "AWS_MCP_URL": getattr(
                    Settings, "AWS_MCP_URL", "https://aws-mcp.us-east-1.api.aws/mcp"
                ),
                "AWS_MCP_REGION": getattr(Settings, "AWS_MCP_REGION", "us-east-1"),
                # Operator-uploaded context files table — read at prompt-build time.
                # grant_context_files_read() is for Lambdas (calls fn.add_environment);
                # the Runtime env is set here directly, and the role grant is below.
                "CONTEXT_FILES_TABLE": foundation.context_files_table.table_name,
                # Tenancy: cluster registry + team membership tables for per-caller
                # visible-cluster resolution. The Runtime resolves identity from a
                # JWKS-verified Cognito ID token the frontend passes in the
                # invocation PAYLOAD (AgentCore's authorizer consumes the inbound
                # Authorization header and forwards neither it nor custom headers to
                # the container), then restricts tool calls to the caller's visible
                # cluster set. USER_POOL_ID/CLIENT_ID are the JWKS issuer + audience
                # for verifying that ID token.
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
                "USER_POOL_ID": foundation.user_pool.user_pool_id,
                "USER_POOL_CLIENT_ID": foundation.user_pool_client.user_pool_client_id,
            },
            network_configuration=agentcore.RuntimeNetworkConfiguration.using_public_network(),
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_cognito(
                user_pool=foundation.user_pool,
                user_pool_clients=[foundation.user_pool_client],
            ),
        )
        # Grant the Runtime's IAM role read access to the context-files table.
        # (grant_context_files_read() is for Lambda fns — it calls fn.add_environment
        # which is unsupported on the Runtime construct. Set env above; grant role here.)
        foundation.context_files_table.grant_read_data(self.runtime.role)
        # Tenancy: allow the Runtime to read cluster registry + team membership.
        foundation.clusters_table.grant_read_data(self.runtime.role)
        foundation.team_members_table.grant_read_data(self.runtime.role)

        # ===== REST API =====

        # Cognito JWT authorizer for the REST API. API Gateway verifies the
        # token signature, issuer, and expiry BEFORE the request reaches any
        # Lambda — so the per-handler base64 decode only ever sees a token the
        # gateway already validated, closing the forged-admin-token hole.
        # Audience is the WebClient id; HTTP API JWT authorizers accept Cognito
        # access tokens by matching the `client_id` claim, which is what the
        # frontend sends (Authorization: Bearer <access_token>).
        jwt_authorizer = apigwv2_authorizers.HttpUserPoolAuthorizer(
            "DbopsJwtAuthorizer",
            foundation.user_pool,
            user_pool_clients=[foundation.user_pool_client],
        )
        # Routes that must NOT carry a Cognito JWT — Slack webhooks authenticate
        # via HMAC signature, and /health is polled by external uptime monitors.
        public_authorizer = apigwv2.HttpNoneAuthorizer()

        self.api = apigwv2.HttpApi(
            self, "HttpApi",
            api_name=f"dbops-{Settings.ENV}-api",
            default_authorizer=jwt_authorizer,
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
            # 128MB (CDK default) ≈ 0.07 vCPU. This handler serializes ~13KB
            # JSON, marshals RDS Data API params/results, and ran at Max Memory
            # ~110/128MB (near the ceiling). 512MB ≈ 0.3 vCPU + headroom cuts the
            # CPU-bound part of the 0.34–0.63s Duration. Cost is memory×duration,
            # offset by the shorter duration; safe, reversible.
            memory_size=512,
            # Retain published versions so the "live" alias has prior versions
            # to roll back to (a deploy publishes a new version + shifts the
            # alias; keeping the old ones is a free rollback target). See the
            # DashboardLiveAlias below — the API integrations point at the alias
            # for zero-downtime deploys.
            current_version_options=lambda_.VersionOptions(
                removal_policy=cdk.RemovalPolicy.RETAIN,
            ),
            # SnapStart cuts cold-init by restoring from a snapshot of the
            # initialized container instead of cold-booting Python+boto3 — the
            # win shows up on scale-out (many users → many fresh containers) and
            # first-load-after-idle. Free for Python (only a small snapshot
            # cache cost). Safe here: the handler creates no boto3 clients /
            # connections / init-time uniqueness at module load, and _LIVE_CACHE
            # is snapshotted EMPTY (the correct starting state). Optimizes the
            # PUBLISHED versions the "live" alias points at (so the integrations
            # must target the alias, which they now do).
            snap_start=lambda_.SnapStartConf.ON_PUBLISHED_VERSIONS,
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                # New: /table-indexes endpoint runs pg_stat_user_indexes against
                # the live target cluster (not the cache DB), so the handler
                # needs to resolve cluster_arn/secret_arn from the registry.
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                # Multi-team tenancy: per-cluster visibility gate + fleet filter.
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        data.cache_db.secret.grant_read(dashboard_lambda)
        data.cache_db.grant_data_api_access(dashboard_lambda)
        foundation.clusters_table.grant_read_data(dashboard_lambda)
        foundation.team_members_table.grant_read_data(dashboard_lambda)
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
                # DocumentDB backup + topology panels — DocDB mirrors the RDS
                # cluster/snapshot API on its own namespace (read-only).
                "docdb:DescribeDBClusters",
                "docdb:DescribeDBClusterSnapshots",
                "docdb:DescribeDBInstances",
                # DynamoDB backup panel — PITR window + on-demand backups (read-only).
                "dynamodb:DescribeContinuousBackups",
                "dynamodb:ListBackups",
                # DynamoDB engine-config panel — table class, SSE, streams, TTL,
                # deletion protection (read-only).
                "dynamodb:DescribeTable",
                "dynamodb:DescribeTimeToLive",
                "cloudwatch:GetMetricStatistics",
                # Hub-spoke: topology/backup/log panels assume the cluster's
                # spoke role to read in its OWN account (local when no role).
                "sts:AssumeRole",
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
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        data.cache_db.secret.grant_read(simulation_lambda)
        data.cache_db.grant_data_api_access(simulation_lambda)
        foundation.clusters_table.grant_read_data(simulation_lambda)
        foundation.team_members_table.grant_read_data(simulation_lambda)
        simulation_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds-data:ExecuteStatement",
                "secretsmanager:GetSecretValue",
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
                "rds:DescribeDBEngineVersions",
                # Live parameter-group metadata for the parameter-change sim
                # (ApplyType/IsModifiable/AllowedValues) — same as the MCP tool.
                "rds:DescribeDBClusterParameters",
                # Real region/edition/instance pricing for scaling cost sims.
                "pricing:GetProducts",
                # Observed Serverless v2 ACU draw for the scaling cost sim.
                "cloudwatch:GetMetricStatistics",
                # ElastiCache node-resize cost sim (EC-5): resolve current node
                # type + count before calling the Price List API. Read-only.
                "elasticache:DescribeReplicationGroups",
                "elasticache:DescribeCacheClusters",
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
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        foundation.clusters_table.grant_read_write_data(clusters_lambda)
        foundation.team_members_table.grant_read_data(clusters_lambda)
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
        # Multi-engine (DynamoDB/DocumentDB) discovery: the clusters API probes for
        # DynamoDB tables and DocumentDB clusters during bulk Discover so non-Aurora
        # engines can be registered alongside Aurora clusters.
        # Note: DocumentDB describe is covered by the existing rds:DescribeDBClusters grant
        # (DocumentDB IAM uses the rds: prefix, not docdb:).
        clusters_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["dynamodb:ListTables", "dynamodb:DescribeTable"],
            resources=["*"],
        ))
        clusters_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "elasticache:DescribeReplicationGroups",
                "elasticache:DescribeCacheClusters",
            ],
            resources=["*"],
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
                "ARCHIVE_BUCKET": data.archive_bucket.bucket_name,
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        data.cache_db.secret.grant_read(reports_lambda)
        data.cache_db.grant_data_api_access(reports_lambda)
        # HTML presigned-URL arm: reports Lambda reads the .html twin from the
        # archive bucket (report_generator already has grant_write on the same
        # bucket). grant_read adds s3:GetObject + s3:ListBucket.
        data.archive_bucket.grant_read(reports_lambda)
        foundation.clusters_table.grant_read_data(reports_lambda)
        foundation.team_members_table.grant_read_data(reports_lambda)

        approvals_lambda = lambda_.Function(
            self, "ApprovalsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/approvals"),
            timeout=cdk.Duration.seconds(30),
            environment={
                "APPROVALS_TABLE": foundation.approvals_table.table_name,
                # enable_data_api 승인-즉시-실행: 레지스트리에서 cluster_arn을
                # 찾아 EnableHttpEndpoint를 호출한다.
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        foundation.approvals_table.grant_read_write_data(approvals_lambda)
        foundation.clusters_table.grant_read_data(approvals_lambda)
        foundation.team_members_table.grant_read_data(approvals_lambda)
        foundation.grant_approval_policy_read(approvals_lambda)  # designated-approver enforcement
        # 의도적으로 rds:EnableHttpEndpoint 단일 액션만 — ModifyDBCluster를
        # 주면 마스터 패스워드 변경·삭제 보호 해제까지 가능한 광범위 권한이
        # 플랫폼에 생긴다. 전용 API(설정 1비트)로 블래스트 반경을 좁히는 것이
        # 이 기능의 보안 전제. Disable은 의도적으로 제외 — 켜는 것만 자동화하고
        # 끄는 것은 사람이 콘솔/CLI에서 하도록 남겨둔다.
        approvals_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["rds:EnableHttpEndpoint"],
                resources=[f"arn:aws:rds:*:{self.account}:cluster:*"],
            )
        )

        # Approval-policies API — admin CRUD over designated-approver policies
        # that the approvals API enforces. Admin-gated + fail-closed in-handler.
        approval_policies_lambda = lambda_.Function(
            self, "ApprovalPoliciesApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/approval_policies"),
            timeout=cdk.Duration.seconds(15),
        )
        foundation.grant_approval_policy_write(approval_policies_lambda)  # R/W + env

        # Context Files API — admin CRUD over operator-uploaded reference text
        # injected into the agent prompt (per-file 32KB, 64KB total budget).
        context_files_lambda = lambda_.Function(
            self, "ContextFilesApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/context_files"),
            timeout=cdk.Duration.seconds(15),
        )
        foundation.grant_context_files_write(context_files_lambda)  # R/W + CONTEXT_FILES_TABLE env

        # Agent Tasks API — list / get / create over the agent-tasks DDB table.
        # Read path backs the /tasks UI + the alert toast deep link; POST writes
        # a pending manual_rca row that the stream worker then executes.
        tasks_lambda = lambda_.Function(
            self, "TasksApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/tasks"),
            timeout=cdk.Duration.seconds(15),
            environment={
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        foundation.grant_task_manage(tasks_lambda)  # agent-tasks R/W + AGENT_TASKS_TABLE env
        foundation.clusters_table.grant_read_data(tasks_lambda)
        foundation.team_members_table.grant_read_data(tasks_lambda)

        # Scheduled Tasks API — CRUD over the scheduled_tasks cache table (the
        # task_scheduler reads these and enqueues agent-tasks when due).
        scheduled_tasks_lambda = lambda_.Function(
            self, "ScheduledTasksApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/scheduled_tasks"),
            timeout=cdk.Duration.seconds(15),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        data.cache_db.secret.grant_read(scheduled_tasks_lambda)
        data.cache_db.grant_data_api_access(scheduled_tasks_lambda)
        foundation.clusters_table.grant_read_data(scheduled_tasks_lambda)
        foundation.team_members_table.grant_read_data(scheduled_tasks_lambda)

        # Config API — admin-edits DB-backed feature toggles (ticketing
        # provider, report delivery) so they flip without a redeploy.
        config_lambda = lambda_.Function(
            self, "ConfigApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/config"),
            timeout=cdk.Duration.seconds(15),
        )
        foundation.grant_app_config_write(config_lambda)  # R/W + APP_CONFIG_TABLE env

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
                # Restore registers the new cluster here (pending_instance) so
                # the restore_finalizer Lambda can complete provisioning.
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        data.cache_db.secret.grant_read(backups_lambda)
        data.cache_db.grant_data_api_access(backups_lambda)
        foundation.clusters_table.grant_read_write_data(backups_lambda)
        backups_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                # CreateDBClusterSnapshot is non-destructive (adds a
                # backup). AddTags lets us stamp dbops:created-by.
                "rds:CreateDBClusterSnapshot",
                # Restore creates a BRAND-NEW cluster from a snapshot or a
                # point in time — it never mutates the source. DescribeDBClusters
                # reads the source VPC/scaling config to clone networking onto
                # the restored cluster.
                "rds:RestoreDBClusterFromSnapshot",
                "rds:RestoreDBClusterToPointInTime",
                "rds:DescribeDBClusters",
                "rds:AddTagsToResource",
                "rds-data:ExecuteStatement",
                "secretsmanager:GetSecretValue",
                # Hub-spoke: snapshot/restore assume the cluster's spoke role so
                # they run in the cluster's OWN account (local when no role).
                "sts:AssumeRole",
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
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        data.cache_db.secret.grant_read(saved_queries_lambda)
        data.cache_db.grant_data_api_access(saved_queries_lambda)
        foundation.clusters_table.grant_read_data(saved_queries_lambda)
        foundation.team_members_table.grant_read_data(saved_queries_lambda)
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
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        data.cache_db.secret.grant_read(alerts_lambda)
        data.cache_db.grant_data_api_access(alerts_lambda)
        data.alert_topic.grant_publish(alerts_lambda)
        foundation.clusters_table.grant_read_data(alerts_lambda)
        foundation.team_members_table.grant_read_data(alerts_lambda)
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

        # Inbound incident webhook (P4) — Datadog / PagerDuty POST an incident;
        # we record it to event_log so it surfaces in the Events panel with a
        # "Chat에서 진단" deep-link. Shared-secret auth (X-DBOps-Webhook-Token);
        # 503 until INCIDENT_WEBHOOK_SECRET is set, 401 on a bad token.
        incident_webhook_lambda = lambda_.Function(
            self, "IncidentWebhookApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/incident_webhook"),
            timeout=cdk.Duration.seconds(10),
            environment={
                "CACHE_DB_CLUSTER_ARN": data.cache_db.cluster_arn,
                "CACHE_DB_SECRET_ARN": data.cache_db.secret.secret_arn,
                "CACHE_DB_NAME": "dbops",
                "INCIDENT_WEBHOOK_SECRET": getattr(
                    Settings, "INCIDENT_WEBHOOK_SECRET", ""
                ),
            },
        )
        data.cache_db.secret.grant_read(incident_webhook_lambda)
        data.cache_db.grant_data_api_access(incident_webhook_lambda)
        # Instant in-app push of external incidents over the WS channel.
        foundation.grant_alert_broadcast(incident_webhook_lambda)

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

        # Zero-downtime deploys under load: point the API integrations at a
        # Lambda ALIAS ("live") that tracks the published current_version,
        # rather than $LATEST. A low-traffic deploy doesn't drop requests, but
        # under a burst of concurrent dashboard loads (many users) during a code
        # swap, $LATEST invocations can transiently fail (observed ~30 API-GW
        # 503s under a 15-way concurrent probe mid-deploy; a clean low-traffic
        # deploy showed 0). With an alias, CDK publishes the new version and
        # shifts the alias only once it is ready — no in-flight gap. This is
        # also the prerequisite for SnapStart, which only optimizes published
        # versions.
        dashboard_alias = lambda_.Alias(
            self, "DashboardLiveAlias",
            alias_name="live",
            version=dashboard_lambda.current_version,
        )

        self.api.add_routes(
            path="/api/dashboard/{cluster_id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/timeseries",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTimeseriesIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/wait-events",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardWaitEventsIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/slow-queries",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSlowQueriesIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/query-detail",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardQueryDetailIntegration", dashboard_alias),
        )
        # Workload diff — pg_stat_statements snapshot delta between two
        # points in time (new / regressed / improved / disappeared
        # queries). Wired from the timeline for "what changed around
        # this deploy".
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/workload-diff",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardWorkloadDiffIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/vacuum-stats",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardVacuumStatsIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/index-recommendations",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardIndexRecsIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/long-running",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardLongRunningIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/batch-timeseries",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardBatchTsIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/blocking-locks",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardBlockingLocksIntegration", dashboard_alias),
        )
        # PG log analytics (CW Logs Insights pass-through). Distinct route
        # from /dashboard/{id} catch-all so API Gateway resolves to the same
        # Lambda but the path-suffix routing in handler.py picks it up.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/log-insights",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardLogInsightsIntegration", dashboard_alias),
        )
        # Capacity forecast (linear regression on metric_snapshots → 30/60/90d
        # projections + days_until_limit).
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/capacity-forecast",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardCapacityForecastIntegration", dashboard_alias),
        )
        # Redundant / prefix-covered / unused indexes (live pg_index query
        # via Data API). PG-only for v1.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/redundant-indexes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardRedundantIndexesIntegration", dashboard_alias),
        )
        # Replication topology (writer + readers + per-instance replica lag,
        # live RDS Describe + CloudWatch).
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/topology",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTopologyIntegration", dashboard_alias),
        )
        # Backup inventory — snapshots + PITR window (read-only tier of
        # the backup workflow). Live RDS Describe calls.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/backups",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardBackupsIntegration", dashboard_alias),
        )
        # Engine-level config (read-only) — DocumentDB cluster settings +
        # DynamoDB table settings the overview panels don't already show.
        # Live docdb/dynamodb Describe calls.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/engine-config",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardEngineConfigIntegration", dashboard_alias),
        )
        # Manual snapshot creation (phase 2 write tier) — admin-gated,
        # routed to the dedicated backups Lambda (not the read-only
        # dashboard handler).
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/snapshot",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("BackupsSnapshotIntegration", backups_lambda),
        )
        # Restore (phase 3 write tier) — snapshot or PITR into a NEW cluster.
        # Same backups Lambda, dispatched by path. admin + type-to-confirm.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/restore",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("BackupsRestoreIntegration", backups_lambda),
        )
        # SLO tracker — availability + latency SLI computed from the cache,
        # error budget burn-down, per-day timeline.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/slo",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSloIntegration", dashboard_alias),
        )
        # Schema lineage / FK graph — live pg_constraint introspection.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/schema-graph",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSchemaGraphIntegration", dashboard_alias),
        )
        # Resource details — engine-specific metadata (DynamoDB table info,
        # DocDB instance list) from cluster_meta.resource_details JSONB.
        # Used by the engine-family dashboard panels.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/resource-details",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardResourceDetailsIntegration", dashboard_alias),
        )
        # Instance list — Aurora cluster member list (writer + readers) for
        # the Compare instance picker. Populated by the meta collector into
        # cluster_meta.instances; cached 30s (membership changes rarely).
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/instances",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration(
                "DashboardInstancesIntegration", dashboard_alias
            ),
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
            ("/api/simulation/dynamodb-capacity-cost", [apigwv2.HttpMethod.POST]),
            ("/api/simulation/elasticache-node-resize", [apigwv2.HttpMethod.POST]),
        ]:
            self.api.add_routes(
                path=sim_path, methods=sim_methods, integration=sim_integration
            )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/settings",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSettingsIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/schema-changes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardSchemaChangesIntegration", dashboard_alias),
        )
        # Unified incident timeline — merges event_log + schema_changes +
        # audit_log into one chronological feed for incident triage.
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/timeline",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTimelineIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/anomalies",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardAnomaliesIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/audit-log",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardAuditLogIntegration", dashboard_alias),
        )
        # 변경 영향 회고 — RDS 변경 이벤트 전후 워크로드 델타. dashboard
        # 라우트는 path별 개별 등록이라(greedy proxy 아님) 새 sub-path는
        # 반드시 여기 추가해야 한다(없으면 핸들러가 구현돼 있어도 404).
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/change-impact",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardChangeImpactIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/multi-cluster/overview",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("MultiClusterOverviewIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/table-sizes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTableSizesIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/table-indexes",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardTableIndexesIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/health-findings",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardHealthFindingsIntegration", dashboard_alias),
        )
        self.api.add_routes(
            path="/api/dashboard/{cluster_id}/extensions",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("DashboardExtensionsIntegration", dashboard_alias),
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
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "TEAM_MEMBERS_BY_USER_INDEX": "by-user",
            },
        )
        foundation.clusters_table.grant_read_data(cost_lambda)
        foundation.team_members_table.grant_read_data(cost_lambda)
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
        cost_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"],
            resources=["*"],  # CloudWatch GetMetricData/ListMetrics don't support resource scoping
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
            authorizer=public_authorizer,
        )
        # Slack slash command — URL paired with the Slack app's
        # `/dbops` command settings. Same signing secret as interactive.
        self.api.add_routes(
            path="/api/slack/command",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("SlackCommandIntegration", slack_command_lambda),
            authorizer=public_authorizer,
        )
        # Inbound incident webhook (Datadog / PagerDuty) — public route,
        # authenticated by the shared-secret token the handler checks.
        self.api.add_routes(
            path="/api/incident-webhook",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("IncidentWebhookIntegration", incident_webhook_lambda),
            authorizer=public_authorizer,
        )
        # DBOps self-monitoring health endpoint — public so external uptime
        # monitors can poll it without a Cognito token.
        self.api.add_routes(
            path="/api/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("HealthIntegration", health_lambda),
            authorizer=public_authorizer,
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
            path="/api/reports/{id}/html",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("ReportHtmlIntegration", reports_lambda),
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
        # Approval policies — admin-gated designated-approver routing
        approval_policies_integration = integrations.HttpLambdaIntegration(
            "ApprovalPoliciesIntegration", approval_policies_lambda
        )
        self.api.add_routes(
            path="/api/approval-policies",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=approval_policies_integration,
        )
        self.api.add_routes(
            path="/api/approval-policies/{id}",
            methods=[apigwv2.HttpMethod.PUT, apigwv2.HttpMethod.DELETE],
            integration=approval_policies_integration,
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
        # Context files — admin-gated operator-uploaded reference text
        context_files_integration = integrations.HttpLambdaIntegration("ContextFilesIntegration", context_files_lambda)
        self.api.add_routes(path="/api/context-files",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=context_files_integration)
        self.api.add_routes(path="/api/context-files/{id}",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=context_files_integration)
        # Agent Tasks — autonomous/scheduled/manual agent work
        tasks_integration = integrations.HttpLambdaIntegration("TasksIntegration", tasks_lambda)
        self.api.add_routes(
            path="/api/tasks",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=tasks_integration,
        )
        # Aggregate stats (counts by status/kind, success rate, avg duration).
        # Registered before /{id} — literal segment takes precedence over path
        # param, but explicit ordering keeps intent unambiguous.
        self.api.add_routes(
            path="/api/tasks/stats",
            methods=[apigwv2.HttpMethod.GET],
            integration=tasks_integration,
        )
        self.api.add_routes(
            path="/api/tasks/{id}",
            methods=[apigwv2.HttpMethod.GET],
            integration=tasks_integration,
        )
        # Scheduled (recurring) agent tasks
        scheduled_integration = integrations.HttpLambdaIntegration(
            "ScheduledTasksIntegration", scheduled_tasks_lambda
        )
        self.api.add_routes(
            path="/api/scheduled-tasks",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
            integration=scheduled_integration,
        )
        self.api.add_routes(
            path="/api/scheduled-tasks/{id}",
            methods=[apigwv2.HttpMethod.DELETE],
            integration=scheduled_integration,
        )
        # App config — admin-gated DB-backed feature toggles
        config_integration = integrations.HttpLambdaIntegration("ConfigIntegration", config_lambda)
        self.api.add_routes(
            path="/api/config",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.PUT],
            integration=config_integration,
        )
        # Onboarding — generates the spoke-account IAM role CloudFormation
        # template an admin deploys so the hub account can assume into it.
        onboarding_lambda = lambda_.Function(
            self, "OnboardingApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/onboarding"),
            timeout=cdk.Duration.seconds(15),
            environment={"HUB_ROLE_ARN": foundation.hub_role.role_arn},
        )
        onboarding_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:GetCallerIdentity"], resources=["*"],
        ))
        self.api.add_routes(
            path="/api/onboarding/template",
            methods=[apigwv2.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("OnboardingIntegration", onboarding_lambda),
        )
        # Admin console — Cognito user & role management (admin-gated)
        admin_users_lambda = lambda_.Function(
            self, "AdminUsersApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/admin_users"),
            timeout=cdk.Duration.seconds(15),
            environment={"USER_POOL_ID": foundation.user_pool.user_pool_id},
        )
        admin_users_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "cognito-idp:ListUsers",
                "cognito-idp:AdminListGroupsForUser",
                "cognito-idp:AdminAddUserToGroup",
                "cognito-idp:AdminRemoveUserFromGroup",
            ],
            resources=[foundation.user_pool.user_pool_arn],
        ))
        admin_users_integration = integrations.HttpLambdaIntegration(
            "AdminUsersIntegration", admin_users_lambda)
        self.api.add_routes(
            path="/api/admin/users",
            methods=[apigwv2.HttpMethod.GET],
            integration=admin_users_integration,
        )
        self.api.add_routes(
            path="/api/admin/users/{username}/role",
            methods=[apigwv2.HttpMethod.POST],
            integration=admin_users_integration,
        )
        # Admin console — Teams CRUD + member/cluster assignment (admin-gated)
        admin_teams_lambda = lambda_.Function(
            self, "AdminTeamsApi",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("../api/admin_teams"),
            timeout=cdk.Duration.seconds(30),
            memory_size=256,
            environment={
                "TEAMS_TABLE": foundation.teams_table.table_name,
                "TEAM_MEMBERS_TABLE": foundation.team_members_table.table_name,
                "CLUSTERS_TABLE": foundation.clusters_table.table_name,
            },
        )
        foundation.teams_table.grant_read_write_data(admin_teams_lambda)
        foundation.team_members_table.grant_read_write_data(admin_teams_lambda)
        foundation.clusters_table.grant_read_write_data(admin_teams_lambda)

        _admin_teams_int = integrations.HttpLambdaIntegration("AdminTeamsIntegration", admin_teams_lambda)
        self.api.add_routes(
            path="/api/admin/teams",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
            integration=_admin_teams_int,
        )
        self.api.add_routes(
            path="/api/admin/teams/{team_id}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
            integration=_admin_teams_int,
        )
        self.api.add_routes(
            path="/api/admin/teams/{team_id}/members/{username}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
            integration=_admin_teams_int,
        )
        self.api.add_routes(
            path="/api/admin/teams/{team_id}/clusters/{cluster_id}",
            methods=[apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST, apigwv2.HttpMethod.DELETE],
            integration=_admin_teams_int,
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
