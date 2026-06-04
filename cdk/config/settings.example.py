class Settings:
    ENV = "dev"
    REGION = "ap-northeast-2"
    ACCOUNT_ID = "123456789012"

    COGNITO_DOMAIN_PREFIX = "dbops-dev"
    CALLBACK_URLS = ["http://localhost:3000/callback"]

    AGENT_MODEL_ID = "anthropic.claude-sonnet-4-20250514-v1:0"
    GATEWAY_SEMANTIC_SEARCH = True

    # AWS Knowledge MCP server — AWS-hosted, public, no-auth streamable-HTTP
    # MCP exposing official AWS/Aurora documentation. The agent connects to
    # it directly (alongside the Gateway) for always-current docs with zero
    # infrastructure. Set empty to disable.
    KNOWLEDGE_MCP_URL = "https://knowledge-mcp.global.api.aws/mcp"

    PI_COLLECTION_INTERVAL_MIN = 1
    STATS_COLLECTION_INTERVAL_MIN = 5

    CACHE_DB_MIN_ACU = 0.5
    CACHE_DB_MAX_ACU = 4

    # Frontend deep-link base URL used by alert dispatchers (Slack button,
    # PagerDuty links). Fill in your CloudFront domain after the first
    # frontend stack deploy — leave empty to disable the deep-link.
    FRONTEND_URL = ""

    # PagerDuty dedup key bucket width. Same rule firing within the window
    # groups into one incident; after the window elapses, a fresh incident
    # opens so on-call sees the alert is still active.
    ALERT_DEDUP_WINDOW_MINUTES = 30

    # Slack signing secret — used by /api/slack/interactive to verify v0
    # HMAC signatures on Block Kit ack button posts. Leave empty to
    # disable Slack two-way ack; the endpoint will refuse all calls with
    # a friendly "not configured" message until you set this. Get it
    # from your Slack app's Basic Information → Signing Secret.
    SLACK_SIGNING_SECRET = ""
