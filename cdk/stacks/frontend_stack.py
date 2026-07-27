import aws_cdk as cdk
from aws_cdk import (
    aws_cloudfront as cloudfront,
)
from aws_cdk import (
    aws_cloudfront_origins as origins,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_s3_deployment as s3_deploy,
)
from aws_cdk import (
    custom_resources as cr,
)
from config.settings import Settings
from constructs import Construct


class FrontendStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, foundation, agent, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self, "FrontendBucket",
            bucket_name=f"dbops-{Settings.ENV}-frontend-{Settings.ACCOUNT_ID}",
            removal_policy=cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,  # cdk-nag AwsSolutions-S10
        )

        rewrite_function = cloudfront.Function(
            self, "PathRewrite",
            code=cloudfront.FunctionCode.from_inline("""
function handler(event) {
    var request = event.request;
    var uri = request.uri;
    if (uri === '/' || uri === '') {
        request.uri = '/index.html';
    } else if (uri.endsWith('/')) {
        request.uri = uri + 'index.html';
    } else if (!uri.includes('.')) {
        request.uri = uri + '.html';
    }
    return request;
}
"""),
        )

        distribution = cloudfront.Distribution(
            self, "Distribution",
            # NB: minimum_protocol_version is intentionally NOT set — it's only
            # valid with a custom ACM cert, and CFN rejects it alongside the
            # default *.cloudfront.net cert. The default cert already pins AWS's
            # modern TLS. cdk-nag AwsSolutions-CFR4 is suppressed accordingly;
            # set min-TLS here once a custom domain is configured.
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=rewrite_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    ),
                ],
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                ),
            ],
        )

        # NOTE: Cognito callback URL for the CloudFront domain must be added manually after
        # first deploy. Open this stack output (DistributionUrl), then add `<url>/callback`
        # to Settings.CALLBACK_URLS in cdk/config/settings.py and redeploy foundation stack.
        # We avoid wiring this automatically because it would create a cyclic
        # frontend->foundation->frontend dependency at synth time.

        # Cache strategy (fixes the "page couldn't load" after a redeploy): a
        # `output: export` SPA is code-split into content-hashed chunks under
        # /_next/static. With NO Cache-Control, the browser heuristically caches
        # the HTML + runtime, so after a redeploy an already-open tab navigates
        # with a STALE chunk manifest and 404s on the new hashed chunks — even
        # though CloudFront's edge was invalidated (that only clears the edge,
        # not the browser's local cache). Fix by splitting the deployment by
        # cache policy so HTML always revalidates and hashed assets are immutable.
        runtime_config = {
            "apiUrl": agent.api.api_endpoint,
            "frontendUrl": f"https://{distribution.distribution_domain_name}",
            "region": Settings.REGION,
            # foundation.cognito_domain_prefix, NOT Settings.COGNITO_DOMAIN_PREFIX:
            # the setting is empty on the documented fresh-account path and
            # FoundationStack derives the real prefix. Reading the raw setting
            # here produced "https://.auth.<region>.amazoncognito.com", a host
            # that resolves nowhere, so login was dead on every fresh deploy.
            "cognitoDomain": f"https://{foundation.cognito_domain_prefix}.auth.{Settings.REGION}.amazoncognito.com",
            "cognitoClientId": foundation.user_pool_client.user_pool_client_id,
            "cognitoUserPoolId": foundation.user_pool.user_pool_id,
            "agentRuntimeArn": agent.runtime.agent_runtime_arn,
            # WebSocket URL for the in-app alert push channel (scoped alert push).
            "webSocketUrl": foundation.ws_stage.url,
        }
        _deploy_common = dict(
            destination_bucket=bucket,
            distribution=distribution,
            distribution_paths=["/*"],
            # prune=False so the three deployments don't delete each other's
            # objects. HTML keeps fixed names (overwritten in place, never
            # stale); hashed chunks accumulate but are immutable + unreferenced
            # once a build rotates, so old ones are harmless.
            prune=False,
        )
        # 1) Content-hashed immutable assets (the whole /_next tree) — cache
        #    forever, never revalidate. Sourced from out/_next and re-prefixed to
        #    /_next so we can scope this deployment to just the hashed assets
        #    (Source.asset has no `include`, only `exclude`).
        s3_deploy.BucketDeployment(
            self, "DeployStaticAssets",
            sources=[s3_deploy.Source.asset("../frontend/out/_next")],
            destination_key_prefix="_next",
            cache_control=[s3_deploy.CacheControl.from_string(
                "public, max-age=31536000, immutable",
            )],
            **_deploy_common,
        )
        # 2) Everything else (HTML, etc., minus /_next) — always revalidate so a
        #    redeploy is picked up on the next request (ETag → 304 or fresh HTML).
        s3_deploy.BucketDeployment(
            self, "DeployHtml",
            sources=[s3_deploy.Source.asset(
                "../frontend/out", exclude=["_next/**"],
            )],
            cache_control=[s3_deploy.CacheControl.from_string("no-cache")],
            **_deploy_common,
        )
        # 3) Runtime config — never cached (login/runtime ARNs must be fresh).
        s3_deploy.BucketDeployment(
            self, "DeployConfig",
            sources=[s3_deploy.Source.json_data("config.json", runtime_config)],
            cache_control=[s3_deploy.CacheControl.from_string("no-store")],
            **_deploy_common,
        )

        # Auto-register the CloudFront callback URL with the Cognito user pool client
        # using an AWS Custom Resource. This avoids the cyclic frontend->foundation
        # dependency that L1 property overrides would create, because the Custom
        # Resource only reads cognito_client_id at deploy time (frontend -> foundation,
        # one-way direction the dependency graph already supports).
        callback_urls = [
            f"https://{distribution.distribution_domain_name}/callback",
            *Settings.CALLBACK_URLS,
        ]
        logout_urls = [
            f"https://{distribution.distribution_domain_name}",
            *[u.rsplit("/callback", 1)[0] for u in Settings.CALLBACK_URLS],
        ]
        update_cognito = cr.AwsCustomResource(
            self, "UpdateCognitoCallbacks",
            on_update=cr.AwsSdkCall(
                service="CognitoIdentityServiceProvider",
                action="updateUserPoolClient",
                parameters={
                    "UserPoolId": foundation.user_pool.user_pool_id,
                    "ClientId": foundation.user_pool_client.user_pool_client_id,
                    "CallbackURLs": callback_urls,
                    "LogoutURLs": logout_urls,
                    "AllowedOAuthFlows": ["implicit"],
                    "AllowedOAuthScopes": ["openid", "profile", "email"],
                    "AllowedOAuthFlowsUserPoolClient": True,
                    "SupportedIdentityProviders": ["COGNITO"],
                    "ExplicitAuthFlows": [
                        "ALLOW_USER_SRP_AUTH",
                        "ALLOW_REFRESH_TOKEN_AUTH",
                        "ALLOW_USER_PASSWORD_AUTH",
                    ],
                },
                physical_resource_id=cr.PhysicalResourceId.of(
                    f"cognito-callback-{distribution.distribution_domain_name}"
                ),
            ),
            policy=cr.AwsCustomResourcePolicy.from_sdk_calls(
                resources=[foundation.user_pool.user_pool_arn]
            ),
            install_latest_aws_sdk=False,
        )
        update_cognito.node.add_dependency(distribution)

        # NOTE: ALLOWED_ORIGINS auto-injection is intentionally omitted.
        # Direct env injection from frontend onto agent Lambdas creates a cyclic
        # frontend->agent->frontend dependency via CloudFormation cross-stack exports
        # (frontend already imports agent's api_endpoint for config.json). Lambdas
        # default to "" → echo request Origin (safe baseline); to harden CORS in
        # production, set ALLOWED_ORIGINS env manually on dashboard/alerts Lambdas
        # to "https://<your-cloudfront>,http://localhost:3000".

        cdk.CfnOutput(self, "DistributionUrl", value=f"https://{distribution.distribution_domain_name}")
        cdk.CfnOutput(self, "ConfigJsonUrl", value=f"https://{distribution.distribution_domain_name}/config.json")
