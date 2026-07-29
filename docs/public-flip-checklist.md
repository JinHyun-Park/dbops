# Making this repository public: pre-flip checklist

The repository is **private** today (`gh repo view` → `PRIVATE`, verified
2026-07-30). This is the gate list for flipping it public. Every line below was
checked against the tree rather than recalled, and the ones already satisfied say
so, because a checklist whose items are mostly already done is only useful if you
can tell which ones those are.

Nothing here is a substitute for reading the diff of whatever you are about to
publish. These are the checks that catch what reading misses.

---

## 1. Secret scan over the FULL history — blocking, and now enforced

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

`.gitleaksignore` currently holds ONE suppression, re-audited 2026-07-30:
`const STORAGE_KEY = "dbops_conversations_v1"` in `chat-panel.tsx`, a
localStorage key name flagged by `generic-api-key`. Not a credential, no
rotation needed.

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

## 2. Deployment-specific config must not be in the tree — already satisfied

`cdk/config/settings.py` is gitignored and holds the operator's real values;
`settings.example.py` is the committed template. Confirm `git ls-files` does not
list `settings.py`, and that `frontend/.env.e2e` (which holds the e2e user's
password) is likewise untracked.

## 3. No hardcoded deployment URLs in test config — already satisfied

`frontend/playwright.config.ts` reads `process.env.DBOPS_E2E_URL` with **no
fallback** and throws when it is unset, so no CloudFront domain is baked in.
(An earlier note claimed a default still needed removing; it does not.)

## 4. Dependency advisories — wired, and cleared 2026-07-30

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

## 5. IaC security lint — already wired

`cdk-nag` (AwsSolutions ruleset) runs in the `cdk-synth` CI job behind
`CDK_NAG=1`, and the synth exit code is the gate, so a new unsuppressed finding
cannot merge. Every suppression carries a reason; read them before publishing,
because a suppression is an argument you are now making in public.

## 6. WS-ticket — see the trigger

The WebSocket `$connect` authorizer's identity source used to be the Cognito
access token in the query string, which is why this checklist is referenced from
the WS section of `BACKLOG.md`. Confirm the current state of that item before
flipping: if the token is still in the URL, **do not enable WS access logging**
and treat the flip as a trigger to finish the ticket pattern, because a public
repo advertises exactly where to look.

## 7. Account ids — a real finding, and YOUR decision

Measured 2026-07-30: the deployment's REAL AWS account id appears in **11 tracked
files** — `BACKLOG.md`, `cdk/cdk.context.json`, eight `docs/superpowers/plans/*`
documents, and one unit test. Everything else is a placeholder
(`123456789012` ×112, `111122223333` ×35, and so on), and the registry rows with
real spoke-role ARNs live in DynamoDB rather than the repo, as they should.

An account id is not a credential: on its own it grants nothing. It does enable
targeted enumeration and it is a building block for social engineering and for
guessing role ARNs, which is why most organisations do not publish theirs.

This was NOT scrubbed, because scrubbing it is a decision with a cost and it is
not one to make on someone's behalf:

- The id is in the COMMIT HISTORY as well as the working tree, so deleting it from
  the current files publishes it anyway. Actually removing it means rewriting
  history (`git filter-repo`), which changes every commit hash after the first
  occurrence.
- `cdk/cdk.context.json` is functional, not documentation: it caches the AZ lookup
  keyed by account, and the `cdk-synth` CI job depends on that cache to synth
  without AWS credentials. Removing the id means either re-caching for a
  placeholder account or giving CI credentials.

So pick one before flipping:

1. **Accept it.** Publish the id. Defensible: it is not a secret, and it is
   already visible to anyone who has seen a support ticket or an ARN from this
   deployment.
2. **Rewrite history.** `git filter-repo` over the 11 paths, re-cache
   `cdk.context.json` for a placeholder account, force-push. Coordinate: every
   existing clone breaks.

Whichever you choose, the spoke-role ARNs of any REGISTERED customer account are a
separate question, and those are in DynamoDB. Confirm nothing has leaked one into
a committed doc.

---

## After the flip

- **GitHub secret scanning + push protection** — free on public repos. Turn both
  on immediately; push protection is the only one of these checks that runs
  before a secret reaches the remote.
- **CodeQL** — free on public repos. Add the default setup for Python and
  JavaScript/TypeScript.
- **Branch protection** — the free tier has no branch protection on private
  repos, which is why Dependabot PRs are merged by hand today. Public unlocks
  it: require the CI jobs above, including `secrets`, before merge.
