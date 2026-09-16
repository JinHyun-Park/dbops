class Settings:
    ENV = "dev"
    REGION = "ap-northeast-2"
    ACCOUNT_ID = "123456789012"

    # Cognito Hosted UI domain prefix. GLOBALLY UNIQUE PER REGION, so a shared
    # literal makes the foundation stack fail with "domain already exists" for
    # everyone except whoever deployed it first. Leave EMPTY and the stack
    # derives dbops-{ENV}-{last 6 of ACCOUNT_ID}, which is unique by
    # construction. Set it only if you want a specific prefix.
    COGNITO_DOMAIN_PREFIX = ""
    CALLBACK_URLS = ["http://localhost:3000/callback"]

    # Bedrock model for the agent. Claude Sonnet 4 has NO bare on-demand model
    # ID: it must be invoked through a cross-region inference profile, so the ID
    # needs a region-family prefix or every chat turn fails with
    # ValidationException. Pick the prefix for YOUR region:
    #   apac.   ap-* regions (this file defaults to ap-northeast-2)
    #   us.     us-* regions
    #   eu.     eu-* regions
    #   global. any region, routed globally
    AGENT_MODEL_ID = "global.anthropic.claude-sonnet-5"

    # Extra regions the in-app model picker scans for available inference profiles,
    # comma-separated. EMPTY means "just REGION", which is the right answer for almost
    # everyone. Only set this if you deliberately want to select a model hosted in a
    # different region than the one you deploy to.
    MODEL_SCAN_REGIONS = ""
    GATEWAY_SEMANTIC_SEARCH = True

    # AWS MCP Server (AWS-managed, SigV4): official AWS/Aurora docs. The agent
    # runtime signs requests with its IAM role and exposes ONLY the read-only
    # doc tools. Regional endpoint (us-east-1 / eu-central-1 only). Empty = doc
    # tools not registered. Replaces the deprecated public knowledge-mcp server.
    AWS_MCP_URL = "https://aws-mcp.us-east-1.api.aws/mcp"
    AWS_MCP_REGION = "us-east-1"

    # PI metrics are collected inside the stats/ETL cycle below: there is no
    # separate Performance Insights cadence (the old PI_COLLECTION_INTERVAL_MIN
    # was never read by any code).
    STATS_COLLECTION_INTERVAL_MIN = 5

    CACHE_DB_MIN_ACU = 0.5
    CACHE_DB_MAX_ACU = 4

    # Archive bucket (S3 Tables / Iceberg + reports) retention. Objects always
    # tier down to cheaper storage (IA → Glacier Instant Retrieval → Deep
    # Archive). Set to a positive number of days to EXPIRE (delete) archived
    # objects after that age for your org's retention policy; 0 = keep forever
    # (default: never auto-delete the audit archive).
    ARCHIVE_RETENTION_DAYS = 0

    # Frontend deep-link base URL used by alert dispatchers (Slack button,
    # PagerDuty links). Fill in your CloudFront domain after the first
    # frontend stack deploy. Leave empty to disable the deep-link.
    FRONTEND_URL = ""

    # PagerDuty dedup key bucket width. Same rule firing within the window
    # groups into one incident; after the window elapses, a fresh incident
    # opens so on-call sees the alert is still active.
    ALERT_DEDUP_WINDOW_MINUTES = 30

    # Slack signing secret: used by /api/slack/interactive to verify v0
    # HMAC signatures on Block Kit ack button posts. Leave empty to
    # disable Slack two-way ack; the endpoint will refuse all calls with
    # a friendly "not configured" message until you set this. Get it
    # from your Slack app's Basic Information → Signing Secret.
    SLACK_SIGNING_SECRET = ""

    # Shared secret for the inbound incident webhook (/api/incident-webhook).
    # Datadog / PagerDuty send it in the X-DBOps-Webhook-Token header; the
    # handler compares it in constant time. Leave empty to disable the endpoint
    # (it returns 503 until set). Use a long random string.
    INCIDENT_WEBHOOK_SECRET = ""

    # Ticketing provider for completed agent tasks (auto-RCA, scheduled
    # reports). "none" (default) keeps ticketing disabled: the task worker's
    # ticketing seam is inert and nothing is created. The provider integration
    # itself is not shipped yet; this is the config switch that will turn it on
    # once a provider is wired (e.g. "jira"). Setting an unwired provider name
    # makes the worker fail the ticketing step loudly rather than silently drop
    # tickets.
    TICKETING_PROVIDER = "none"

    # Push generated report digests to managed Slack subscribers + the SNS
    # topic (email). False (default) keeps report delivery off so existing
    # alert subscribers don't start receiving daily reports. Turn on once you
    # want scheduled reports delivered, not just viewable in /reports.
    REPORT_DELIVERY_ENABLED = False

    # Target cluster for the failure-scenario demo page (/scenarios). Empty
    # (default) leaves the page listing the scenarios with every button disabled
    # and the reason shown, so the feature is off until you opt a cluster in.
    #
    # Pick a cluster you are happy to have synthetic incident rows written
    # against, because that is what a scenario does: it INSERTs signal rows into
    # the cache tables (metric_snapshots, event_log, blocking_locks, query_stats,
    # schema_snapshots) for that cluster_id and then runs a real RCA over them.
    # The rows are self-purging and the TARGET DATABASE IS NEVER TOUCHED, but the
    # cluster's cached history does carry the injected rows until they age out, so
    # a cluster whose dashboards you use for real capacity decisions is the wrong
    # choice. A registered demo cluster is the right one.
    #
    # The schema-change scenario additionally needs PostgreSQL: schema_snapshots
    # is PostgreSQL-only by decision, so on a MySQL target that one button
    # refuses with an explanation and the rest still work.
    SCENARIO_CLUSTER_ID = ""

    # Model that writes the RCA narrative and recommendations. Empty (default)
    # reuses AGENT_MODEL_ID, so an existing deployment behaves exactly as before.
    #
    # Worth separating from the chat model: chat runs per turn and is
    # latency-sensitive, while this runs ONCE per incident and produces the
    # report a DBA acts on. Measured 2026-09-15 on an identical prompt,
    # claude-opus-5 spent 673 output tokens against claude-sonnet-5's 668 and
    # stated the evidentiary limit of a single-signal diagnosis, which the
    # smaller model did not. A few Opus calls a day is a cheap way to buy that.
    #
    # Must be a profile the deploy account can invoke; check with
    # `aws bedrock list-inference-profiles --type-equals SYSTEM_DEFINED`.
    RCA_NARRATIVE_MODEL_ID = ""
