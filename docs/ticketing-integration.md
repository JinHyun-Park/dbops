# Ticketing Integration

DBOps can file a ticket (Jira, ServiceNow, …) when an agent task completes —
e.g. an auto-RCA on an incident opens a tracking ticket and stores its URL on
the task record. This is shipped as an **integration seam, not a working
integration**: the wiring, config toggle, and failure handling are all in place,
but no concrete provider is bundled, so you wire the one your team uses.

By default ticketing is **off** and the task flow is unchanged.

## What's already in place

- **In-app toggle** — `TICKETING_PROVIDER` is an admin-editable setting in the
  web UI under **Configure → Settings** (no redeploy to flip; stored in the
  app-config table, read by `get_config`). Default `none`.
- **The seam** — `mcp-servers/mcp_servers/workers/ticketing.py`:
  `get_provider().create_ticket(...)` is called by the task worker after a task
  finishes. `create_ticket` returns the ticket URL (str) or `None`.
- **Fail-safe behavior** — the worker isolates any provider exception, so
  ticketing can never break task completion. A provider _named_ in config but
  not yet implemented resolves to `_UnwiredProvider`, which raises on use — so a
  misconfiguration fails loudly (logged) instead of silently dropping tickets.

## Wiring a real provider (recipient steps)

Ticketing toggled on without a shipped implementation just raises (caught,
logged) — these four steps make it real. Example: Jira.

**1. Implement a `TicketProvider`** in `ticketing.py` (or a new module imported
there). Override `create_ticket` to call your provider's REST API and return the
created ticket's URL:

```python
import os, json, urllib.request, base64

class JiraTicketProvider(TicketProvider):
    name = "jira"

    def create_ticket(self, *, task_id, cluster_id, kind, summary, result):
        base = get_config("TICKETING_BASE_URL", os.environ["TICKETING_BASE_URL"])
        project = get_config("TICKETING_PROJECT_KEY", "OPS")
        # token comes from Secrets Manager (step 3), injected as an env var
        auth = base64.b64encode(os.environ["TICKETING_TOKEN"].encode()).decode()
        body = json.dumps({"fields": {
            "project": {"key": project},
            "summary": f"[{kind}] {cluster_id}: {summary}"[:240],
            "description": json.dumps(result, ensure_ascii=False, default=str)[:30000],
            "issuetype": {"name": "Task"},
        }}).encode()
        req = urllib.request.Request(
            f"{base}/rest/api/2/issue", data=body, method="POST",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            key = json.loads(r.read())["key"]
        return f"{base}/browse/{key}"
```

**2. Register it** in the `_IMPLEMENTED` map (keyed by lower-case config name):

```python
_IMPLEMENTED = {"jira": JiraTicketProvider}
```

**3. Provide credentials + grant access.** Create a Secrets Manager secret for
the API token and grant the **operations** Lambda (the task worker's home) read
on it, in CDK — never hardcode the token. Inject it as the `TICKETING_TOKEN` env
var (and set `TICKETING_BASE_URL` / `TICKETING_PROJECT_KEY` either as env or
app-config). Mirror how an existing secret is granted in `cdk/stacks/`.

**4. Enable it.** In the web UI, **Configure → Settings**, set
`TICKETING_PROVIDER` to `jira` (admin only). Steps 1–3 require a redeploy; the
toggle itself does not.

## Notes

- `create_ticket` receives `task_id`, `cluster_id`, `kind`, `summary`, and the
  full `result` dict — shape your ticket fields from these.
- Keep the call cheap and fast; the worker runs it inline after a task. Heavy or
  flaky providers should enqueue rather than block.
- ServiceNow / others follow the same pattern: subclass, register, grant the
  secret, toggle.
