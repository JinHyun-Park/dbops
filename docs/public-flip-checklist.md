# Making this repository public: pre-flip checklist

**The repository is PUBLIC as of 2026-08-11, published at commit `de4d164`.** This was
the gate list for flipping it, kept as the record of what was checked and what was
found. The post-flip settings are applied; see the final section. Every line below was
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
Re-measured 2026-08-03 at the publish candidate `988fc8f`, 961 commits /
11.68 MB: **no leaks found**.

`.gitleaks.toml` extends the default ruleset rather than replacing it, and
allowlists exactly one path: `.gitleaksignore`, which documents each suppression
in a comment that quotes the flagged line and therefore re-reports the very
string it exists to explain. Before that exclusion a clean scan still ended in
`leaks found: 1` pointing at the allowlist file, which trains a reader to ignore
the exit status.

`.gitleaksignore` holds THREE suppressions, each re-audited at the publish
candidate on 2026-08-03 by deleting the file and reading what came back. All three
are `generic-api-key` hits on entropy alone. None is a credential and none needs
rotation:

| suppressed hit                                             | what it actually is                                                                                 |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `STORAGE_KEY` in `chat-panel.tsx`                          | the browser localStorage key the chat history is stored under                                       |
| the same constant quoted in THIS file                      | the self-reference described below                                                                  |
| a JWT-shaped literal in `tests/unit/api/test_ws_ticket.py` | synthetic. Its payload segment decodes to `{"kid":"real-looking"}` and the rest is a literal `.x.y` |

The JWT one is only in HISTORY: the working tree replaced that literal with a
non-JWT-shaped string, because the assertion is that the `token` param is no longer
an identity source, so the value's shape carries no test value and a realistic one
trips the scanner forever.

An earlier version of this section claimed the scan "returns 0 findings both times,
so the suppressions are inert rather than load-bearing." That was wrong, and it is
worth stating plainly because it is the kind of claim a reader of a public repo will
check. Deleting `.gitleaksignore` produces `leaks found: 3`, matching the three
recorded fingerprints exactly. The suppressions ARE load-bearing; what makes them
safe is that each suppressed finding was individually opened and verified, not that
they suppress nothing.

Two traps that produced that wrong claim, worth avoiding on the next audit:

- `gitleaks ... | tail` reports `tail`'s exit status, so a piped run prints
  `exit=0` while gitleaks is failing. Read the `leaks found:` line, not `$?`.
- A JSON report (`--report-format json --report-path ...`) is the only way to see
  WHICH findings were suppressed. The console summary gives a count.

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

**Re-run 2026-08-11, at the flip itself: 0 became 1 high again.** A new advisory
(GHSA-2v37-7h3g-55p8, `nanoid <3.3.17`, "custom generators can loop indefinitely when
size is zero") landed in the week since the last run. It reached the PRODUCTION tree,
not just dev tooling: the path is `next@16.2.12` to `postcss@8.5.25` to `nanoid`, and
Next declares postcss itself, so `--omit=dev` flagged it too.

Cleared with an `overrides` entry at `nanoid: ^3.3.18`, deliberately inside the 3.x
line. postcss declares `nanoid ^3.3.16`, so the bump is in range; nanoid 5.x and 6.x
are ESM-only and would break postcss's CommonJS require, which is the same mistake the
removed brace-expansion override made. Real runtime exposure was nil either way
(postcss runs at build time and `nanoid` appears nowhere in `out/`), but a live
production-tree advisory is visible to everyone the moment the repo is public.

That is twice now that this gate moved between one run and the next. Treat a passing
`npm audit` as valid for hours, not days: re-run it in the same sitting as the flip.

**Third recurrence of the stale-floor pattern, found 2026-08-11 by re-reading the
overrides rather than trusting the 0 result.** `postcss: ^8.5.18` had drifted INSIDE
GHSA-fxqj-rqcc-2cmp (range `<=8.5.22`). Audit reported 0 only because the lockfile
happened to resolve 8.5.25; the declared floor no longer excluded a vulnerable version,
so any fresh resolve could have picked one. Raised to `^8.5.23`, the first version
outside the range. `sharp: ^0.35.3` is outside its advisory (`<0.35.0`) but its floor
equals the installed version, so it has zero headroom and will need the same check.

The lesson generalizes: **a passing `npm audit` does not validate the overrides.** Audit
judges what resolved; an override is a claim about what is ALLOWED to resolve. Read every
floor against the advisory it was added for, every time.

Two overrides are deliberately outside the declared range, and both depend on
facts that could change:

- `sharp ^0.35.3` sits outside next's optional `^0.34.5`. Safe ONLY because
  `next.config` sets `output: "export"` and `images: { unoptimized: true }`, so
  sharp is never invoked. If image optimization is ever enabled, drop the override
  and re-check compatibility first.
- `brace-expansion ^5.0.8` was REMOVED on 2026-08-04, and it is the clearest example
  in this file of an override doing harm. A new advisory (GHSA-rgw5-rvv9-x895, high,
  range `4.0.0 - 5.0.8`) landed on the exact pinned version, so the override was
  inside the vulnerable range it existed to escape. Bumping it to `^5.0.9` cleared
  audit but broke lint: v5 changed its export shape, and the v3-era `minimatch`
  bundled under `@eslint/config-array` calls it as a function (`TypeError: expand is
not a function`). Removing the override entirely is correct: npm then installs
  1.1.18 for the old consumer and 5.0.9 for the new one side by side, `npm audit`
  reports 0, and lint runs. A forced major across the tree is not a safe default.

### `npm run lint` could not run at all, found 2026-08-04

Not a security gate, but a fresh clone running `npm run lint` got a stack trace
rather than findings, and for a project distributed as "deploy this to your own AWS
account" that is a first-impression breaker.

Cause: `eslint` was on `^10` while `eslint-config-next@16.2.12` bundles
`eslint-plugin-react ^7.37.0`, whose newest release (7.37.5) declares
`eslint: ^3 || ... || ^9.7`. No published version supports ESLint 10. It went
unnoticed because `eslint-config-next` declares `eslint: >=9.0.0` with no upper
bound, so npm never warned, and NOTHING gates lint: CI runs build and typecheck
only, and pre-commit runs prettier, ruff, tsc and gitleaks.

Pinned to `eslint ^9` (9.39.5), which is what the toolchain actually supports. Lint
now runs. It reports 113 problems (97 errors, 16 warnings), almost all
`Calling setState synchronously within an effect`, a rule that arrived with Next 16
and accumulated unseen for as long as eslint was crashing. Those are recorded in
BACKLOG as an open item; they are code quality, not a publish blocker, and fixing 97
effect call sites is not a checklist step.

Deliberately NOT added to CI yet: a lint gate that is red on the first run trains
people to ignore it. Add it after the findings are addressed.

### The Python audit, RUN 2026-08-03, and it found the one real thing

This was the last genuinely open gate. `pip-audit 2.10.1`, in a throwaway venv so
the brew Python is untouched.

All 13 `requirements*.txt` files: **no known vulnerabilities**. That result is also
the trap, because it is nearly meaningless on its own: those files carry unbounded
`>=` floors, so pip-audit resolves them to whatever is current at audit time, which
is not what ships.

What ships for the agent is `agent/_deps/`, a vendored tree frozen at whatever
`build-deps.sh` last produced. Auditing the ACTUAL pinned versions there (reconstruct
them from the `*.dist-info` directory names) returned **8 vulnerable packages, 20
distinct advisories**:

| package           | shipped | fixed in |
| ----------------- | ------- | -------- |
| pyjwt             | 2.12.1  | 2.13.0   |
| starlette         | 1.0.0   | 1.3.1    |
| mcp               | 1.27.1  | 1.28.1   |
| python-multipart  | 0.0.28  | 0.0.31   |
| bedrock-agentcore | 1.9.0   | 1.18.1   |
| cryptography      | 48.0.0  | 48.0.1   |
| idna              | 3.14    | 3.15     |
| pydantic-settings | 2.14.1  | 2.14.2   |

`pyjwt` is the one that matters most: `agent/tenancy.py` uses it to verify the
caller's Cognito id_token against the pool JWKS, which is the tenancy isolation
boundary.

The cause was not a missed advisory, it was a stale artifact. `agent/requirements.txt`
ALREADY declared `PyJWT[crypto]>=2.13.0` and `mcp>=1.28.1`, both ABOVE what was
vendored: the floors were raised and `_deps` was never rebuilt, so the shipped image
contradicted its own requirements file. `_deps/` is gitignored, so nothing in CI or
in a diff could see it.

Fixed by re-running `agent/build-deps.sh`, which cleared all 20. Verified at three
levels rather than assumed:

1. Re-audit of the rebuilt tree: no known vulnerabilities.
2. The DEPLOYED artifact: downloaded the runtime's own S3 code zip and read the
   `dist-info` names inside it. All 8 patched versions are in the artifact the
   runtime actually runs (version 57).
3. The runtime BOOTS on them: a real `/chat` message produced
   `Gateway token issued`, `Loaded 65 tools from Gateway` (so mcp 1.29.0 speaks to
   the Gateway) and `AgentCore Memory wired (actor=...)`, where the actor id is
   derived from the verified id_token, so pyjwt 2.13.0's JWKS path ran.

Two traps this exposed, both worth carrying forward:

- **Newer pip byte-compiles into `--target`.** The rebuild produced 295
  `__pycache__` directories and +23MB where the previous tree had zero, and a
  `__pycache__` under `agent/` is the known cause of an AgentCore image rejection.
  `build-deps.sh` now passes `--no-compile` and sweeps afterwards.
- **A green e2e suite is not agent verification.** The smoke suite's RCA test
  asserts the drawer's UI and produces ZERO runtime log events, so it passes with a
  dead agent. Verifying the agent needs a real message plus the runtime log group.

Re-run both halves before publishing, not just the requirements half.

## 5. IaC security lint, already wired

`cdk-nag` (AwsSolutions ruleset) runs in the `cdk-synth` CI job behind
`CDK_NAG=1`, and the synth exit code is the gate, so a new unsuppressed finding
cannot merge. Every suppression carries a reason; read them before publishing,
because a suppression is an argument you are now making in public.

**Read 2026-08-03, and three of them were arguing for a design that no longer
ships.** Both WebSocket suppressions said the `$connect` authorizer reads the
Cognito access token from the query string, and one cited a `HARDENING GUARD`
comment in `foundation_stack.py`. The WS-ticket pattern replaced the token on
2026-07-30 and that comment is gone: the identity source is now
`route.request.querystring.ticket`, a random 60-second single-use ticket that
`$connect` spends before any log line is written. So the honest reason for APIG1 is
"nothing consumes the logs", not "logging would leak a credential", and enabling
access logging is now safe rather than forbidden. APIG4 likewise described a
"Cognito Lambda authorizer" where the authorizer validates a ticket.

**A third stale reason, found 2026-08-11: `AwsSolutions-IAM5`.** It said wildcards were
"confined to inherently account/region-wide control-plane calls (rds:Describe*,
GetMetric*, ListTables)", which reads as read-only-only. Measured: 32 `resources=["*"]`
statements across agent_stack (26) and data_stack (6), containing ~25 MUTATING actions
(rds cluster/instance/snapshot/parameter-group/custom-endpoint writes, dynamodb capacity
and TTL and PITR, elasticache modify/reboot/failover, bedrock inference profiles, ssm
parameters, and `rds-data:ExecuteStatement`). This was the most security-sensitive claim
in the repo and it understated the grant.

Rewritten to state all three groups at real size, why the writes cannot be
resource-scoped (targets are operator-registered databases in arbitrary accounts,
resolved from the registry at request time and not enumerable at synth), and what does
bound them (the fail-closed `approval_guard`, plus the spoke role's trust policy and its
`ManagedBy=dbops` tag condition). One part of the finding was wrong and is worth noting:
`sts:AssumeRole` IS scoped, to `arn:aws:iam::*:role/dbops-spoke-role`, in both
foundation_stack.py:282 and data_stack.py:203.

Corrected in `cdk/app.py`. The lesson generalizes past this repo: a suppression
reason is a claim about code, and code moves. Re-read every reason against the
current source before publishing, do not re-read the reasons against each other.

The remaining reasons were checked and hold. The one open product decision is
Cognito MFA (`AwsSolutions-COG2`), deliberately out of scope: it is a policy call
for whoever deploys this.

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
no `BEGIN * PRIVATE KEY`, no real JWT, no `sk-`/`ghp_`/`xox` tokens, no
`AWS_BEARER_TOKEN_BEDROCK`, no real Slack webhook, no DB password, no Cognito
user pool id anywhere.

"No real JWT" is the precise claim. History does hold ONE JWT-shaped literal, in
`tests/unit/api/test_ws_ticket.py`, and it is synthetic: its payload segment
decodes to `{"kid":"real-looking"}` and the remainder is a literal `.x.y`. It
carries no pool id, no client id and no subject. See section 1 for why the
suppression count claim here was previously wrong.

So there is nothing in history a rewrite would remove except AWS resource NAMING.
A rewrite is the wrong instrument for that: it changes every commit hash, and 27
in-repo SHA citations across 21 tracked files would silently rot (10 in
`BACKLOG.md`, 6 in `tests/unit/data_pipeline/test_etl_baselines.py`, 5 in
`.gitleaksignore`, whose fingerprints are `commit:path:rule:line`-scoped and would
be voided outright).

Counted before `BACKLOG.md` was cleared to a closed-out record on 2026-08-03,
which removed its 10 citations. That lowers the count to 17 across 20 files; it
does not change the conclusion, and `.gitleaksignore` alone still voids.

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

### Committer email: CLOSED 2026-08-04, accepted as published

Not a gate and not an open item. The owner reviewed the exposure and accepted it.
Recorded so it stops being re-raised, and so the reasoning is available if it is
ever revisited.

**Forward fix, already in place.** This repo's local git identity is
`<id>+<login>@users.noreply.github.com`, GitHub's own linked-noreply form, so new
commits keep contribution attribution and profile links while the private mailbox
stops appearing. `--global` was deliberately left alone; revert the repo-scoped
change with `git config --unset user.email`.

**History: published as is.** Measured 2026-08-04: 862 commits carry a personal
address as author and 866 as committer, against 39 on the noreply form (25
Dependabot, 14 post-fix). Accepted because the exposure is a low-severity privacy
item (bulk harvesting and cross-linking of one mailbox), not a credential and not a
product vulnerability, and it adds no contact path a public repo does not already
provide.

**In-place removal was never available, which is why there is no runbook here.**
Rewriting `main` and force-pushing does NOT remove the addresses from this
repository. GitHub retains `refs/pull/*` permanently and a branch force-push does
not touch them: measured 79 pull refs on the remote, and the ancestor chain of
`refs/pull/40/head` alone keeps 681 of the personal-address commits reachable, so
`GET /repos/.../commits/<old-sha>` keeps answering. The only route that worked was
publishing a mailmap-rewritten mirror as a NEW repository and deleting this one,
which also costs every commit SHA (the remaining in-repo SHA citations rot, and the
three commit-scoped `.gitleaksignore` fingerprints would need regenerating, not
deleting, per section 1). That was declined as disproportionate to a low-severity
item. `git log --format=%ae` reconstructs the mailmap if the decision is ever
reversed.

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

## 9. DEPLOYED and verified live 2026-07-31

All four stacks deployed in dependency order, one `cdk deploy` process at a time:
foundation (38s), data (53s), agent (114s), frontend (139s). Every stack
UPDATE_COMPLETE.

**The Cognito fix is live, and the regression path is proven closed.** Checked with
`describe-user-pool-client` twice, before and after the frontend deploy, because
the frontend custom resource is the thing that could undo it and CloudFormation
cannot see an SDK-side mutation:

|                              | after foundation | after frontend |
| ---------------------------- | ---------------- | -------------- |
| `PreventUserExistenceErrors` | `ENABLED`        | `ENABLED`      |
| access / id validity         | 720 minutes      | 12 hours       |

The unit flip from `minutes` to `hours` is the evidence, not a cosmetic detail: it
shows the custom resource DID run and DID apply the re-asserted values. Without
the section 8 change that same call would have omitted both properties and reset
them to Cognito's defaults, which is exactly the silent regression this was about.

Also verified live after the frontend deploy: `/config.json` returns HTTP 200 with
all of `apiUrl`, `cognitoClientId`, `cognitoUserPoolId`, `webSocketUrl`, and the
site root returns 200. That settles the `prune=False` claim empirically rather than
by reading the CDK source.

**The tag preflight is live, and it fired for real.** Probed the deployed
operations Lambda on preview-only paths (`approved=false`, so no write and no
approval consumed):

- 7 of the wired tools reached their `approval_required` preview with **zero
  crashes**. That was the actual regression risk: this change added imports to 18
  tool files, and one bad import would have made that tool dark.
- On same-account clusters, no `warning` key appears. Correct: the cross-account
  gate runs first, so the preflight costs nothing and claims nothing.
- On `dbops-xacct-demo`, the one registered cross-account cluster, the warning
  FIRED: the tool assumed the spoke role, described the cluster, read its tags, and
  reported that `ManagedBy=dbops` is absent and that `rds:ModifyDBCluster` would
  therefore be denied AFTER the single-use approval is consumed.

That last line is also an operational finding worth acting on: **the real spoke
cluster genuinely lacks the tag today**, so a write against it right now would burn
an approval and fail. Tag it, or confirm your spoke role does not use the template's
tag condition.

Scope of that evidence, stated precisely: `manage_maintenance` is the live
end-to-end proof of the whole chain (gate, ARN resolution, tag read, warning text).
The other 12 call sites share that same helper and are covered by the unit tests
and mutation checks plus the live no-crash probe, not by an individual live warning.

### The commands, for the next time

```bash
cd cdk
cdk deploy dbops-dev-foundation   # then data, agent, frontend, in that order
```

Pre-flight checks that mattered: no IAM or security-group changes in any stack;
`agent/` free of `__pycache__` (its presence fails the AgentCore Runtime image);
`frontend/out/` freshly built, because CDK ships the prebuilt export and does not
build for you.

<details>
<summary>What was pending before this deploy (kept for the diff record)</summary>

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

</details>

---

## After the flip: DONE 2026-08-11

All applied and verified through the API immediately after the flip.

| setting                     | state                                                                                               |
| --------------------------- | --------------------------------------------------------------------------------------------------- |
| Secret scanning             | `enabled`                                                                                           |
| Push protection             | `enabled` (the only check that runs BEFORE a secret reaches the remote)                             |
| Dependabot security updates | `enabled` (needed `vulnerability-alerts` enabled first; the bare call 422s)                         |
| CodeQL default setup        | `configured`, both `Analyze (python)` and `Analyze (javascript-typescript)` running                 |
| Branch protection on `main` | 4 required checks, `strict=true`, force-push and deletion blocked, conversation resolution required |

Two deliberate choices in the branch protection, both to avoid changing a solo
maintainer's workflow without being asked:

- `enforce_admins: false`, so the owner can still push to `main` directly. Verified by
  an actual push after applying it. Turning this on would require a PR for every
  change.
- `required_pull_request_reviews: null`, because a review requirement nobody can
  satisfy would block every merge on a single-maintainer repo.

Did NOT take effect and is worth knowing: `secret_scanning_non_provider_patterns` and
`secret_scanning_validity_checks` both accepted the PATCH but read back `disabled`.
They appear to need GitHub Advanced Security rather than the free public tier.

First results: GitHub's own secret scanning found **0** alerts, independent
confirmation of the gitleaks result on the same tree. CodeQL opened **3**, all
preliminarily false positives, recorded in BACKLOG for proper triage. Code scanning
alerts are visible only to collaborators, so nothing there is publicly exposed.

One avoidable artifact: verifying that owner pushes still work was done with an empty
commit (`92c78ff`) instead of `git push --dry-run`, and force-push is now blocked, so
it is permanent. Harmless, but use `--dry-run` next time.
