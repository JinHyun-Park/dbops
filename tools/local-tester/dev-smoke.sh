#!/usr/bin/env bash
# Dev-env live smoke — READ-ONLY, best-effort. Refreshes a Cognito token from
# the local Playwright session (frontend/e2e/.auth/state.json) and curls a few
# key API endpoints. Prints 'SMOKE: PASS' / 'SMOKE: FAIL' (or 'SMOKE: SKIP' when
# no usable session — never fails the tester just because a session expired).
#
# This is a STARTER smoke (only /api/clusters). Extend ENDPOINTS for richer
# coverage. Config resolution, in order: DBOPS_API_URL / DBOPS_COGNITO_CLIENT_ID
# env vars, else the deployed runtime config at frontend/out/config.json (which
# `npm run build` + deploy leaves behind, and which is gitignored). The refresh
# token comes from the local, gitignored state.json.
#
# NOTHING deployment-specific is hardcoded here. This file previously carried one
# operator's live API Gateway URL and Cognito app-client id as defaults. Neither
# is a credential (both ship in any browser session), but a tracked default makes
# a public repo advertise one specific live deployment, and it silently pointed
# every other clone's smoke run at that same deployment instead of their own.
set -uo pipefail

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "SMOKE: SKIP (not a repo)"; exit 0; }

CFG="$REPO/frontend/out/config.json"
cfg_key() {  # $1 = key; "" when the file or key is absent
    [ -f "$CFG" ] || return 0
    python3 -c "
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], '') or '')
except Exception:
    pass" "$CFG" "$1" 2>/dev/null
}

API="${DBOPS_API_URL:-$(cfg_key apiUrl)}"
CLIENT_ID="${DBOPS_COGNITO_CLIENT_ID:-$(cfg_key cognitoClientId)}"
REGION="${AWS_REGION:-ap-northeast-2}"
ENDPOINTS=("/api/clusters")

# SKIP, never FAIL: this smoke is best-effort by contract, and an unconfigured
# checkout is not a test failure. Same posture as the missing-session skips below.
if [ -z "$API" ] || [ -z "$CLIENT_ID" ]; then
    echo "SMOKE: SKIP (no API URL / Cognito client id — set DBOPS_API_URL and" \
         "DBOPS_COGNITO_CLIENT_ID, or build+deploy the frontend so $CFG exists)"
    exit 0
fi
STATE="$REPO/frontend/e2e/.auth/state.json"
[ -f "$STATE" ] || { echo "SMOKE: SKIP (no $STATE)"; exit 0; }

RT="$(python3 -c "import json,sys
for o in json.load(open(sys.argv[1])).get('origins',[]):
    for kv in o.get('localStorage',[]):
        if kv['name'].endswith('.refreshToken'):
            print(kv['value']); raise SystemExit" "$STATE" 2>/dev/null)"
[ -z "$RT" ] && { echo "SMOKE: SKIP (no refresh token in session)"; exit 0; }

TOK="$(aws cognito-idp initiate-auth --auth-flow REFRESH_TOKEN_AUTH \
  --client-id "$CLIENT_ID" --auth-parameters REFRESH_TOKEN="$RT" \
  --region "$REGION" --query 'AuthenticationResult.IdToken' --output text 2>/dev/null)"
{ [ -z "$TOK" ] || [ "$TOK" = "None" ]; } && { echo "SMOKE: SKIP (token refresh failed — session likely expired)"; exit 0; }

FAIL=0
for path in "${ENDPOINTS[@]}"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: $TOK" "$API$path" 2>/dev/null)"
  echo "  GET $path -> $code"
  [ "$code" = "200" ] || FAIL=1
done
[ "$FAIL" = 0 ] && echo "SMOKE: PASS" || echo "SMOKE: FAIL"
exit 0
