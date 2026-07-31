# Making this repository public: pre-flip checklist

The repository is **private** today (`gh repo view` → `PRIVATE`, verified
2026-07-30). This is the gate list for flipping it public. Every line below was
checked against the tree rather than recalled, and the ones already satisfied say
so, because a checklist whose items are mostly already done is only useful if you
can tell which ones those are.

Nothing here is a substitute for reading the diff of whatever you are about to
publish. These are the checks that catch what reading misses.

---

## 1. Secret scan over the FULL history, blocking, and now enforced

A public repo exposes its entire commit history, not its tip. A secret that was
committed and later deleted is still published.

**Status: enforced in CI.** `.github/workflows/ci.yml` has a `secrets` job that
runs `gitleaks detect` over the whole history on every push and PR, with
`fetch-depth: 0` (a shallow clone would scan only the tip and report clean for a
secret still in history) and `--redact` (so the CI log does not itself become the
leak). The exit code is the gate.

Measured 2026-07-30 on 900 commits / 11.25 MB: **no leaks found**.

`.gitleaks.toml` extends the default ruleset rather than replacing it, and
allowlists exactly one path: `.gitleaksignore`, which documents each suppression
in a comment that quotes the flagged line and therefore re-reports the very
string it exists to explain. Before that exclusion a clean scan still ended in
`leaks found: 1` pointing at the allowlist file, which trains a reader to ignore
the exit status.

`.gitleaksignore` currently holds ONE suppression, re-audited 2026-07-30: the
`STORAGE_KEY` constant in `chat-panel.tsx`, which names the browser localStorage
key the chat history is stored under and is flagged by `generic-api-key` purely on
entropy. Not a credential, no rotation needed.

The literal is deliberately NOT quoted here. Quoting it made this very document a
finding on the next run, which is the same self-reference that put
`.gitleaksignore` in the allowlist. Describe flagged strings; do not reproduce
them.

Verified the gate still bites, per credential shape:

| planted shape                            | result                                                                        |
| ---------------------------------------- | ----------------------------------------------------------------------------- |
| AWS access key id + random secret        | caught (`aws-access-token`, `generic-api-key`)                                |
| Bedrock bearer token literal             | caught (`generic-api-key`)                                                    |
| GitHub PAT                               | caught (`github-pat`)                                                         |
| Slack webhook URL                        | caught (`slack-webhook-url`)                                                  |
| RSA / OPENSSH / PKCS8 private key blocks | caught (`private-key`, all three)                                             |
| AWS's own documented EXAMPLE key         | NOT caught, and that is correct: the default ruleset allowlists known dummies |

Two scan-mode notes that will otherwise waste your time:

- Use git mode (the default). `--no-git` walks the working tree including
  `frontend/node_modules`, which returned **5222** findings and is meaningless
  here: `node_modules` is gitignored and 0 of its files are tracked, so it is
  never published.
- A single-line fake private key body is NOT detected. That is a probe artifact,
  not a gap; a realistic body is caught. Do not conclude the rule is broken.

**Before flipping:** re-run the scan on the exact commit you are publishing, and
if it finds anything, ROTATE the credential first. Removing it from history does
not un-leak it.

## 2. Deployment-specific config must not be in the tree, already satisfied

`cdk/config/settings.py` is gitignored and holds the operator's real values;
`settings.example.py` is the committed template. Confirm `git ls-files` does not
list `settings.py`, and that `frontend/.env.e2e` (which holds the e2e user's
password) is likewise untracked.

## 3. No hardcoded deployment URLs in test config, already satisfied

`frontend/playwright.config.ts` reads `process.env.DBOPS_E2E_URL` with **no
fallback** and throws when it is unset, so no CloudFront domain is baked in.
(An earlier note claimed a default still needed removing; it does not.)

## 4. Dependency advisories, wired, and cleared 2026-07-30

`.github/dependabot.yml` covers three ecosystems weekly: `npm` (`/frontend`),
`pip`, and `github-actions`.

`npm audit` was at **5 high** when this checklist was written, not the 0 it had
been left at: ten of the eleven advisories were Next.js 16.0-16.2.10
(middleware/proxy bypass, SSRF in Server Actions and in rewrites, DoS in Server
Actions, cache confusion, Image-Optimization DoS). Cleared to **0** by
`next@16.2.12` (a PATCH bump inside 16.2.x) plus three overrides. Re-run it before
flipping; a public repo makes the dependency set public too.

Do NOT run `npm audit fix --force`: it downgrades Next to 9.x. The established
pattern is an `overrides` entry in `frontend/package.json`. Note that an override
can go stale in a way that reads as handled: `postcss: ^8.5.10` was still inside
the range of the sourceMappingURL path-traversal advisory (<=8.5.17), so when you
see an override, check it still covers the advisory it was added for.

Two overrides are deliberately outside the declared range, and both depend on
facts that could change:

- `sharp ^0.35.3` sits outside next's optional `^0.34.5`. Safe ONLY because
  `next.config` sets `output: "export"` and `images: { unoptimized: true }`, so
  sharp is never invoked. If image optimization is ever enabled, drop the override
  and re-check compatibility first.
- `brace-expansion ^5.0.8` comes solely from eslint tooling, so it is dev-only.

There is no Python audit tool installed on the dev machine (`pip-audit` is
absent). Install it for the one-off pre-flip run rather than assuming Dependabot's
weekly PRs have covered everything.

## 5. IaC security lint, already wired

`cdk-nag` (AwsSolutions ruleset) runs in the `cdk-synth` CI job behind
`CDK_NAG=1`, and the synth exit code is the gate, so a new unsuppressed finding
cannot merge. Every suppression carries a reason; read them before publishing,
because a suppression is an argument you are now making in public.

## 6. WS-ticket, already satisfied (verified 2026-07-31)

This gate said "confirm the current state before flipping: if the access token is
still in the WebSocket URL, do not enable WS access logging". It is no longer
conditional. Verified in the code, not recalled:

- `api/ws_authorizer/handler.py` reads `queryStringParameters["ticket"]` and spends
  it with a conditional `delete_item` (`attribute_exists(ticket)` plus
  `ReturnValues="ALL_OLD"`), so two concurrent handshakes on one ticket leave
  exactly one winner. Missing, unknown, spent, expired, or unconfigured all Deny.
  No Cognito call remains in the path.
- `api/ws_ticket/handler.py` mints the ticket (60-second `expires_at`, checked in
  code because DynamoDB TTL lags up to 48h), wired at `/api/ws-ticket` in
  `agent_stack.py`.
- `frontend/src/lib/alert-stream.ts` mints via the normal bearer-authed POST and
  connects with `?ticket=`.

So no bearer token is in any URL, and WS access logging is no longer dangerous to
enable. One correction made while verifying: `alert-stream.ts`'s own header comment
still described the OLD `?token=` design, which would have told a reader the exact
opposite of the truth. Fixed.

## 7. Identifiers: RESOLVED 2026-07-31, working tree scrubbed, history kept

Decision, taken after an 8-agent audit (5 identifier-class sweeps over all 847
tracked files and all 953 commits on all 193 refs, then 3 adversarial review
lenses): **scrub the working tree, do NOT rewrite history.**

### What made the history question answerable

The audit's headline is a definitive negative, verified two independent ways
(path grep over `git rev-list --all --objects`, and a per-commit tree scan):

> `cdk/config/settings.py` was **never** committed, on any of the 193 refs, at any
> point in 953 commits. `.gitignore` has listed it since the commit that created
> `.gitignore`.

Nor was any `.env*`, `frontend/out/`, `cdk.out/`, or runtime `config.json`. And
credential-class material is **zero**: no AKIA/ASIA/ABIA/ACCA/AIDA/AROA key ids,
no `BEGIN * PRIVATE KEY`, no JWTs, no `sk-`/`ghp_`/`xox` tokens, no
`AWS_BEARER_TOKEN_BEDROCK`, no real Slack webhook, no DB password, no Cognito
user pool id anywhere. `gitleaks` over all commits, run twice (once with the 3
`.gitleaksignore` fingerprints DISABLED), returns 0 findings both times, so the
suppressions are inert rather than load-bearing.

So there is nothing in history a rewrite would remove except AWS resource NAMING.
A rewrite is the wrong instrument for that: it changes every commit hash, and 27
in-repo SHA citations across 21 tracked files would silently rot (10 in
`BACKLOG.md`, 6 in `tests/unit/data_pipeline/test_etl_baselines.py`, 5 in
`.gitleaksignore`, whose fingerprints are `commit:path:rule:line`-scoped and would
be voided outright).

### What was scrubbed from the working tree

| Identifier                                      | Where                              | Action                                   |
| ----------------------------------------------- | ---------------------------------- | ---------------------------------------- |
| Hub AWS account id                              | 11 files, 12 occurrences           | → `123456789012`                         |
| Spoke AWS account id                            | `BACKLOG.md`                       | → `210987654321`                         |
| CloudFront distribution id                      | 15 plan docs                       | → placeholder                            |
| S3 frontend bucket name (embeds the account id) | 8 plan docs                        | → placeholder                            |
| AgentCore Gateway id                            | `agent_stack.py` comment, 1 spec   | → placeholder                            |
| Live API Gateway URL + Cognito app-client id    | `tools/local-tester/dev-smoke.sh`  | defaults REMOVED, see below              |
| Live CloudFront domain                          | `.audit-light-mode-and-refresh.md` | file untracked, moved to the notes vault |
| Local absolute paths (`/Users/<name>/…`)        | 30 occurrences in docs             | → `<repo>`                               |

`cdk/cdk.context.json` is now **gitignored** rather than scrubbed in place. CDK
keys context by account id, so a tracked copy republishes the account on every
lookup. The one entry CI needs (the `settings.example.py` account's AZ list) moved
into `cdk/cdk.json`'s `context` block, which the CLI treats as read-only. This
also satisfies the CDK guide's instruction that context be committed: the value is
still committed, in the other committed file.

### Three claims in the first version of this section were WRONG

Recorded because each was stated confidently and each was falsified by
measurement, not by argument:

1. **"`cdk.context.json` is functional; removing the id means re-caching for a
   placeholder account or giving CI credentials."** False, and the reason is
   subtle: whether synth needs the real-account entry or the placeholder one
   depends on which `settings.py` is present. CI copies `settings.example.py`, so
   CI only ever needs the `123456789012` entry; the real-account entry is for the
   owner's local synth and can stay local and untracked. Measured on a clean
   archive of HEAD with credentials, config and IMDS all unavailable:
   `cdk synth --no-lookups` exits 0 with the file absent and the entry in
   `cdk.json`, produces all 4 templates, passes the `CDK_NAG=1` gate, and the CLI
   does **not** recreate `cdk.context.json`.
2. **"Deleting it from the current files publishes it anyway, so a rewrite buys
   the appearance of removal."** A rationalisation. This repo is private, has one
   human author, no forks, and the 57 `refs/remotes/pr/*` refs are the owner's own
   fetch config, so there is no third-party clone population. A pre-publication
   rewrite would in fact be complete. The sound reason to skip it is the first
   one: an account id is not a credential. (The _real_ version of this concern:
   force-pushing a rewrite to the same GitHub repo leaves the old objects
   reachable by SHA through the web UI and API until GitHub runs gc. If a rewrite
   ever happens, publish as a fresh repo.)
3. **"The spoke-role trust policy names a specific hub role ARN."** It does not.
   `cdk/cross-account/spoke-role-template.yaml` uses
   `Principal.AWS: !Sub arn:aws:iam::${HubAccountId}:root`, i.e. it trusts the
   whole hub ACCOUNT, and the boundary is that account plus each hub role's
   identity policy. The conclusion is unchanged (an outsider still needs
   credentials inside the hub account) but the stated mechanism was wrong.

### The genuinely serious finding was not the account id

See section 8. The attacker-lens reviewer ignored the account id entirely and went
straight for an unauthenticated Cognito call.

### Committer email: forward-fixed, history left alone

Measured: **868 of 954 commits** carry a personal address, and a different one from
the GitHub account email (the other 86 are Dependabot's own noreply). Publishing
would link that mailbox to this repo permanently, and it is the one item editing
files cannot touch.

Resolved 2026-07-31 in the proportionate direction rather than the maximal one:

- **Future commits: fixed.** This repo's local git identity is now
  `<id>+<login>@users.noreply.github.com`, GitHub's own linked-noreply form, so
  contribution attribution and profile links keep working while the private
  mailbox stops appearing. `--global` was deliberately left alone; this is a
  repo-scoped change, revert with `git config --unset user.email`.
- **Existing 868 commits: not rewritten.** The repo is private with 0 forks, so
  nothing is exposed today, and a rewrite costs every commit SHA: 27 in-repo SHA
  citations across 21 tracked files rot silently. Worse, per claim 2 above, a
  force-push to the SAME repo leaves the old objects reachable by SHA through the
  GitHub UI and API until gc runs, so doing it now would buy the appearance of
  removal, which is exactly the reasoning this document rejects elsewhere.

**If you do flip public and want the historical addresses gone, that is the moment,
and it has to be done as a fresh repo.** Runnable procedure:

```bash
# 1. mailmap: map the old identity to the noreply one
printf 'JinHyun-Park <NEW@users.noreply.github.com> <OLD@personal.address>\n' > /tmp/mailmap
# 2. rewrite a CLONE, never the working repo
git clone --mirror . /tmp/dbops-rewrite && cd /tmp/dbops-rewrite
git filter-repo --mailmap /tmp/mailmap          # remaps in-message SHAs by default
# 3. publish as a NEW empty GitHub repo, do NOT force-push over the old one
# 4. then re-check: the 3 .gitleaksignore fingerprints are commit-scoped and are
#    now void. They are INERT today (measured: gitleaks with them disabled = 0
#    findings), so delete them rather than regenerate.
# 5. and fix the 27 in-repo SHA citations, or accept them as dangling references
```

---

## 8. Cognito user enumeration: FIXED 2026-07-31, and it was never about going public

The most serious thing the audit found had nothing to do with the account id, and
was exploitable **before** any flip.

The web app client is secretless (`generate_secret=False`) and its id ships inside
the publicly served frontend bundle, so it is readable by anyone who loads the
site. That is fine by design. What was not fine: `prevent_user_existence_errors`
was never set anywhere in the repo, so Cognito applied its API default (`LEGACY`)
and returned `UserNotFoundException` for an unknown email versus
`NotAuthorizedException` for a wrong password. The error **code alone** told an
anonymous caller whether an email was registered: free user enumeration, no
credentials, no browser.

Verified at the template level rather than in the source, because "the property is
missing from the Python" and "the property is missing from the deployed client" are
different claims: the synthesized `AWS::Cognito::UserPoolClient` carried no
`PreventUserExistenceErrors` at all.

Fixed in two places, and the second is the interesting one:

- `foundation_stack.py`: `prevent_user_existence_errors=True`.
- `frontend_stack.py`: the `UpdateCognitoCallbacks` custom resource calls
  `updateUserPoolClient`, which is a **full replace**: every optional property it
  omits reverts to the API default. Setting the flag only in `foundation_stack`
  would have survived exactly until the next `cdk deploy dbops-{env}-frontend`.
  The same omission was already silently reverting the deliberate 12-hour access
  and id token validity to Cognito's 60-minute default on every frontend deploy.
  Both are now re-asserted in that call.

`tests/cdk/test_synth.py` pins all of it (4 tests, mutation-checked): the flag in
the foundation template, the 720-minute validity, and the custom resource carrying
every property. Fixing only one half looks correct in isolation and is undone in
practice, so both halves are asserted.

`USER_PASSWORD_AUTH` was deliberately KEPT despite one reviewer recommending its
removal. Removing it does not remove the online password-guessing surface, because
`user_srp=True` is an equally unauthenticated password flow for anyone holding the
client id, and `smoke-test.sh` authenticates through it to obtain an id_token
without a browser. Rate limiting and MFA are the controls for guessing; this
setting is the control for ENUMERATION. **MFA is not configured on this pool**,
and that is a product decision, still open, and out of scope for a flip checklist.

**These are CDK changes: inert until `cdk deploy dbops-{env}-foundation` and
`dbops-{env}-frontend` run.** Deploy foundation first, then frontend.

---

## 9. Pending deploy: the section 8 fix is committed but NOT live

Everything above is in the repo. The Cognito change is IaC, so it does nothing
until deployed. `cdk diff` was run 2026-07-31 against the live dev environment and
all four stacks have pending changes:

| Stack                  | Pending change                                                                                                                | Why                                                                                                                 |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `dbops-dev-foundation` | 1 resource: `UserPoolClient` gains `PreventUserExistenceErrors: ENABLED`                                                      | the section 8 fix. This is the whole diff, nothing else                                                             |
| `dbops-dev-data`       | 6 Lambda code updates (ETLCollector, RdsDirectCollector, EventProcessor, ReportGenerator, ProactiveMonitor, RestoreFinalizer) | asset rebuilds from the dependency floor bumps; resolved versions are unchanged, so no behaviour change is expected |
| `dbops-dev-agent`      | 5 MCP Lambdas + the AgentCore Runtime                                                                                         | the ManagedBy tag-preflight wiring, plus an image rebuild from the `agent/requirements.txt` floors                  |
| `dbops-dev-frontend`   | 2 bucket deployments + the `UpdateCognitoCallbacks` custom resource                                                           | the rebuilt static export, and the custom resource now re-asserting every client property                           |

Checked before deploying, so these do not need re-checking:

- **No IAM or security-group changes in any stack**, so `--require-approval never`
  approves nothing security-relevant.
- **`agent/` carries no `__pycache__`** (0 files). Its presence makes the AgentCore
  Runtime image deploy fail, so this is the pre-flight for the agent stack.
- **`frontend/out/` is freshly built** (`npm run build`, all routes prerendered).
  The frontend stack ships the prebuilt export; CDK does not build for you.
- **`config.json` is safe.** All three `BucketDeployment`s set `prune=False`, and
  `config.json` is its own deployment via `Source.json_data`, so a frontend deploy
  cannot delete the runtime config out from under the login flow.

Deploy in dependency order, one `cdk deploy` process at a time (two concurrent
processes race on `cdk.out` and one fails silently):

```bash
cd cdk
cdk deploy dbops-dev-foundation --require-approval never   # the security fix
cdk deploy dbops-dev-data       --require-approval never
cdk deploy dbops-dev-agent      --require-approval never   # ~10 min for a warm Runtime
cdk deploy dbops-dev-frontend   --require-approval never
```

Then verify the fix is actually live, because CloudFormation reporting success and
the client being correct are different claims (the frontend custom resource mutates
the client through the SDK, outside CloudFormation's view):

```bash
aws cognito-idp describe-user-pool-client \
  --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
  --query 'UserPoolClient.{prevent:PreventUserExistenceErrors,access:AccessTokenValidity,id:IdTokenValidity,units:TokenValidityUnits}'
```

Expect `prevent: ENABLED` and 12-hour access/id validity. Run this AFTER the
frontend deploy, not just after foundation: the frontend stack is the one that
could undo it, and that is the whole point of section 8.

---

## After the flip

- **GitHub secret scanning + push protection**, free on public repos. Turn both
  on immediately; push protection is the only one of these checks that runs
  before a secret reaches the remote.
- **CodeQL**, free on public repos. Add the default setup for Python and
  JavaScript/TypeScript.
- **Branch protection**, the free tier has no branch protection on private
  repos, which is why Dependabot PRs are merged by hand today. Public unlocks
  it: require the CI jobs above, including `secrets`, before merge.
