#!/bin/bash
# Post-deploy smoke test. Hits the live API endpoints and key Lambdas to verify
# the platform is end-to-end functional. Returns non-zero on any failure.
set -u

# ENV and REGION come from cdk/config/settings.py, the same source cdk/app.py
# uses to name the stacks. Hardcoding dev + ap-northeast-2 made every
# describe-stacks miss on any other deployment, so the smoke test exited 1 and
# reported a red result for a deployment that had actually succeeded.
SMOKE_DIR="$(cd "$(dirname "$0")" && pwd)"
read -r DBOPS_ENV DBOPS_REGION <<EOF
$(cd "$SMOKE_DIR/cdk" && python3 -c "from config.settings import Settings; print(Settings.ENV, Settings.REGION)")
EOF
: "${DBOPS_ENV:?could not read ENV from cdk/config/settings.py}"
: "${DBOPS_REGION:?could not read REGION from cdk/config/settings.py}"
export AWS_REGION="$DBOPS_REGION"


PASS=0
FAIL=0
WARN=0

green() { printf '\033[32m%s\033[0m\n' "$1"; }
red() { printf '\033[31m%s\033[0m\n' "$1"; }
amber() { printf '\033[33m%s\033[0m\n' "$1"; }

pass() { PASS=$((PASS + 1)); green "  ✓ $1"; }
fail() { FAIL=$((FAIL + 1)); red   "  ✗ $1"; }
warn() { WARN=$((WARN + 1)); amber "  ⚠ $1"; }

echo "========================================="
echo "  DBOps Smoke Test"
echo "========================================="

# Every /api route sits behind the API Gateway Cognito JWT authorizer (only the
# two Slack webhooks and /health are public), so an unauthenticated curl gets 401
# BEFORE the Lambda runs. The old checks used `curl -fsS ... || echo 0`, which
# turned that 401 into CLUSTER_COUNT=0 and then reported
# "/api/clusters: 0 clusters registered" as a PASS on a fleet of 11 clusters, and
# skipped every per-cluster check as "nothing registered". A green smoke test
# that never reached a single authenticated route is worse than a red one.
#
# So: acquire a real id token when local e2e credentials exist (the same
# viewer-role user the Playwright suite uses; GET is all we need), and SKIP the
# authenticated checks loudly when they do not. Never pass them blind.
ID_TOKEN=""
if [ -f "$SMOKE_DIR/frontend/.env.e2e" ]; then
  # shellcheck disable=SC1091
  set -a; . "$SMOKE_DIR/frontend/.env.e2e"; set +a
  CLIENT_ID=$(aws cloudformation describe-stacks --region "$DBOPS_REGION" \
    --stack-name "dbops-$DBOPS_ENV-foundation" \
    --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \
    --output text 2>/dev/null)
  if [ -n "${DBOPS_E2E_EMAIL:-}" ] && [ -n "${DBOPS_E2E_PASSWORD:-}" ] && [ -n "$CLIENT_ID" ]; then
    ID_TOKEN=$(aws cognito-idp initiate-auth --region "$DBOPS_REGION" \
      --auth-flow USER_PASSWORD_AUTH --client-id "$CLIENT_ID" \
      --auth-parameters "USERNAME=$DBOPS_E2E_EMAIL,PASSWORD=$DBOPS_E2E_PASSWORD" \
      --query 'AuthenticationResult.IdToken' --output text 2>/dev/null)
    [ "$ID_TOKEN" = "None" ] && ID_TOKEN=""
  fi
fi
if [ -n "$ID_TOKEN" ]; then
  pass "authenticated as the e2e viewer user"
else
  warn "no id token (frontend/.env.e2e missing or auth failed) — authenticated API checks are SKIPPED, not passed"
fi

# GET an authenticated route. Echoes the body on success, empty on any failure,
# and NEVER substitutes a default that a later test could read as success.
api_get() {
  [ -z "$ID_TOKEN" ] && return 1
  curl -fsS -H "Authorization: Bearer $ID_TOKEN" "$API_URL$1" 2>/dev/null
}

# 1. Discover stack outputs
API_URL=$(aws cloudformation describe-stacks \
  --region "$DBOPS_REGION" \
  --stack-name "dbops-$DBOPS_ENV-agent" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" \
  --output text 2>/dev/null)
WEB_URL=$(aws cloudformation describe-stacks \
  --region "$DBOPS_REGION" \
  --stack-name "dbops-$DBOPS_ENV-frontend" \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionUrl'].OutputValue" \
  --output text 2>/dev/null)

[ -n "$API_URL" ] && [ "$API_URL" != "None" ] && pass "agent stack API URL: $API_URL" || { fail "agent stack ApiUrl not found"; exit 1; }
[ -n "$WEB_URL" ] && [ "$WEB_URL" != "None" ] && pass "frontend stack URL: $WEB_URL" || fail "frontend DistributionUrl not found"

API_URL=${API_URL%/}
WEB_URL=${WEB_URL%/}

# 2. /config.json delivered + has expected keys
CONFIG=$(curl -fsS "$WEB_URL/config.json" 2>/dev/null)
if [ -n "$CONFIG" ]; then
  for key in apiUrl cognitoClientId region agentRuntimeArn cognitoDomain; do
    echo "$CONFIG" | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin).get('$key') else 1)" 2>/dev/null \
      && pass "/config.json has $key" || fail "/config.json missing $key"
  done
  # cognitoDomain needs more than a truthiness check. An empty Hosted-UI prefix
  # yields "https://.auth.<region>.amazoncognito.com": a truthy string whose
  # first host label is EMPTY, so it resolves nowhere and login is dead while
  # every other check stays green. Assert the host has no empty label.
  echo "$CONFIG" | python3 -c "
import json, sys
from urllib.parse import urlparse
host = urlparse(json.load(sys.stdin).get('cognitoDomain') or '').hostname or ''
labels = host.split('.')
sys.exit(0 if len(labels) >= 2 and all(labels) else 1)
" 2>/dev/null \
    && pass "/config.json cognitoDomain host is well-formed" \
    || fail "/config.json cognitoDomain host is malformed (empty Hosted-UI prefix? nobody can log in)"
else
  fail "/config.json not reachable"
fi

# 3. /api/clusters (DynamoDB → REST). A failed call must NOT become a count of 0.
if [ -z "$ID_TOKEN" ]; then
  CLUSTER_COUNT=""
  warn "/api/clusters skipped (no token)"
else
  CLUSTER_COUNT=$(api_get "/api/clusters" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "")
  if [ -n "$CLUSTER_COUNT" ]; then
    pass "/api/clusters: $CLUSTER_COUNT clusters registered"
  else
    fail "/api/clusters unreachable or not JSON"
  fi
fi

# 4. /api/multi-cluster/overview
if [ -z "$ID_TOKEN" ]; then
  warn "/api/multi-cluster/overview skipped (no token)"
else
  MULTI=$(api_get "/api/multi-cluster/overview" | python3 -c "import json,sys; print(len(json.load(sys.stdin).get('clusters',[])))" 2>/dev/null || echo "")
  [ -n "$MULTI" ] && pass "/api/multi-cluster/overview: $MULTI clusters" || fail "multi-cluster overview unreachable"
fi

# 5. /api/alert-rules + /api/alert-subscriptions
if [ -z "$ID_TOKEN" ]; then
  warn "/api/alert-rules and /api/alert-subscriptions skipped (no token)"
else
  api_get "/api/alert-rules" >/dev/null && pass "/api/alert-rules reachable" || fail "alert-rules unreachable"
  SUB_TOPIC=$(api_get "/api/alert-subscriptions" | python3 -c "import json,sys; print(json.load(sys.stdin).get('topic_arn',''))" 2>/dev/null)
  [ -n "$SUB_TOPIC" ] && pass "alert SNS topic: $SUB_TOPIC" || fail "alert subscriptions endpoint missing topic_arn"
fi

# 6. Pick first cluster and exercise dashboard endpoints
# These are authenticated too, so without a token every one of them returned 401
# and the whole section reported "no clusters registered yet" as a benign warning
# on a fleet that had 11.
FIRST_CID=""
if [ -n "$ID_TOKEN" ]; then
  FIRST_CID=$(api_get "/api/clusters" | python3 -c "
import json,sys
rows = json.load(sys.stdin)
print(rows[0]['cluster_id'] if rows else '')
" 2>/dev/null)
fi
if [ -z "$ID_TOKEN" ]; then
  warn "per-cluster dashboard checks SKIPPED (no token)"
elif [ -z "$FIRST_CID" ]; then
  warn "no clusters visible to the smoke user — register a cluster, or check that the smoke user's team can see one"
else
  pass "smoke testing against cluster: $FIRST_CID"
  for path in "" "/timeseries?metric=cpu&hours=1" "/wait-events?hours=1" "/slow-queries?hours=1" "/vacuum-stats" "/long-running" "/blocking-locks" "/settings" "/batch-timeseries?metrics=cpu,aas&hours=1"; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
      -H "Authorization: Bearer $ID_TOKEN" "$API_URL/api/dashboard/$FIRST_CID$path")
    [ "$STATUS" = "200" ] && pass "GET /api/dashboard/{cid}$path → 200" || fail "GET /api/dashboard/{cid}$path → $STATUS"
  done
fi

# 7. Schema migrator: NO file may report errors.
# Two bugs used to make this check meaningless: (a) `grep -q "errors=0"` passed
# whenever ANY of the last lines was clean, and the migrator prints one line per
# SQL file across 25+ files, so a real failure was masked by its clean siblings;
# (b) `head -1` often selected the CDK Provider *framework* function, which never
# prints "errors=" at all, so the check silently degraded to the warn branch.
# Exclude the framework function and fail on any nonzero error count.
MIG_FN=$(aws lambda list-functions --region "$DBOPS_REGION" --max-items 1000 \
  --query "Functions[?contains(FunctionName, 'SchemaMigrator') && !contains(FunctionName, 'Providerframework')].FunctionName" \
  --output text 2>/dev/null | tr '\t' '\n' | head -1)
if [ -n "$MIG_FN" ]; then
  MIG_LINES=$(aws logs filter-log-events \
    --region "$DBOPS_REGION" \
    --log-group-name "/aws/lambda/$MIG_FN" \
    --start-time $(($(date +%s) * 1000 - 86400000)) \
    --query "events[?contains(message, 'errors=')].message" \
    --output text 2>/dev/null | tr '\t' '\n')
  if [ -z "$MIG_LINES" ]; then
    warn "SchemaMigrator has no run in the last 24h (it only runs when the asset changes)"
  elif echo "$MIG_LINES" | grep -qE "errors=[1-9]"; then
    fail "SchemaMigrator reported errors: $(echo "$MIG_LINES" | grep -E 'errors=[1-9]' | head -3 | tr '\n' ' ')"
  else
    pass "SchemaMigrator: $(echo "$MIG_LINES" | grep -c 'errors=') file(s), all errors=0"
  fi
else
  fail "SchemaMigrator Lambda not found"
fi

echo ""
echo "========================================="
green "  ✓ $PASS passed"
if [ "$WARN" -gt 0 ]; then amber "  ⚠ $WARN warnings"; fi
if [ "$FAIL" -gt 0 ]; then red "  ✗ $FAIL failed"; fi
echo "========================================="

[ "$FAIL" -eq 0 ]
