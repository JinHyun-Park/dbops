# Audit Export Unbounded (Cursor Pagination) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the `/activity` audit export retrieve all matching approval rows (no 500 cap) via opt-in cursor pagination on `GET /api/activity`, with the timeline UI unchanged.

**Architecture:** Add a paginated mode to `GET /api/activity` (single DDB scan page + `next_cursor`), and an export client loop that accumulates all pages, sorts client-side, and builds the CSV with the existing `buildAuditCsv`.

**Tech Stack:** Python 3.12 Lambda (DynamoDB scan), Next.js 16 (static export), TypeScript.

## Global Constraints

- **No `Co-Authored-By: Claude` trailer** in any commit (user rule).
- **Additive / backward-compatible:** the default (no `cursor`, no `export`) `GET /api/activity` behavior — full `_scan_all` → sort `created_at` desc → truncate to `min(limit,500)` → `{items, count}` — must be byte-for-byte unchanged. The timeline UI must not change.
- **DDB scan `Limit` is applied BEFORE `FilterExpression`** — a filtered page can return fewer items than the page size (even 0) while still having a `LastEvaluatedKey`. The export loop therefore continues while `next_cursor != null`, NOT while a page is non-empty.
- **Cursor is opaque:** `next_cursor = base64.urlsafe_b64encode(json.dumps(LastEvaluatedKey))`; a malformed `cursor` → `400 invalid cursor`, never a 500.
- **No new infra / no new route:** same `/api/activity`, same authorizer.
- **Korean UI copy** for any user-facing text; keep DBA jargon in English.

---

### Task 1: Backend — cursor pagination mode on `GET /api/activity`

**Files:**

- Modify: `api/approvals/handler.py` (the `if method == "GET" and path.endswith("/activity"):` block, ~lines 191-250)
- Test: `tests/unit/api/test_activity.py` (extend — it already exists)

**Interfaces:**

- Produces: `GET /api/activity?export=true[&cursor=<b64>][&limit=<n>]` → `{"items": [...compact...], "count": <page len>, "next_cursor": "<b64>"|null}`. Default mode (no cursor/export) unchanged: `{"items", "count"}`. Filters (`cluster_id`, `actor`, `action_type`) apply in both modes.

- [ ] **Step 1: Read the current `/activity` block** in `api/approvals/handler.py` (~lines 191-250) to see the exact filter-building + `compact` projection + the `_scan_all`/sort/truncate + `_created_ms` usage. Confirm `import base64` and `import json` are present at the top (they are — `_decode_jwt_payload` uses base64).

- [ ] **Step 2: Write the failing tests.** Extend `tests/unit/api/test_activity.py`. Read the file first to reuse its event/handler-loading helpers (it loads the handler via importlib and patches the DDB table). Add tests that patch the table's `scan` for paginated mode. Use this shape (adapt the handler/table access + event builder to the file's existing helpers):

```python
def _activity_event(qsp=None):
    return {
        "requestContext": {"http": {"method": "GET"}},
        "rawPath": "/api/activity",
        "headers": {"authorization": "Bearer hdr.e30.sig"},  # any token; /activity GET is not admin-gated
        "queryStringParameters": qsp or {},
    }


def test_export_first_page_returns_next_cursor(monkeypatch):
    # Page 1: scan returns 2 items + a LastEvaluatedKey -> next_cursor present.
    import base64, json as _json
    monkeypatch.setenv("APPROVALS_TABLE", "t")
    fake = MagicMock()
    fake.scan.return_value = {
        "Items": [
            {"approval_id": "a2", "created_at": "2", "approval_status": "approved"},
            {"approval_id": "a1", "created_at": "1", "approval_status": "approved"},
        ],
        "LastEvaluatedKey": {"approval_id": "a1", "created_at": "1"},
    }
    with patch.object(handler.boto3, "resource") as res:
        res.return_value.Table.return_value = fake
        r = handler.lambda_handler(_activity_event({"export": "true"}), None)
    assert r["statusCode"] == 200
    body = json.loads(r["body"])
    assert body["count"] == 2
    assert body["next_cursor"]  # non-null
    # the cursor decodes back to the LEK
    assert _json.loads(base64.urlsafe_b64decode(body["next_cursor"])) == {"approval_id": "a1", "created_at": "1"}
    # scan was a single page (Limit set), not _scan_all
    assert fake.scan.call_count == 1
    assert fake.scan.call_args.kwargs.get("Limit")


def test_export_last_page_null_cursor(monkeypatch):
    monkeypatch.setenv("APPROVALS_TABLE", "t")
    fake = MagicMock()
    fake.scan.return_value = {"Items": [{"approval_id": "a0", "created_at": "0", "approval_status": "consumed"}]}  # no LEK
    cursor = __import__("base64").urlsafe_b64encode(b'{"approval_id":"a1","created_at":"1"}').decode()
    with patch.object(handler.boto3, "resource") as res:
        res.return_value.Table.return_value = fake
        r = handler.lambda_handler(_activity_event({"export": "true", "cursor": cursor}), None)
    body = json.loads(r["body"])
    assert body["next_cursor"] is None
    assert body["count"] == 1
    # the decoded cursor was passed as ExclusiveStartKey
    assert fake.scan.call_args.kwargs.get("ExclusiveStartKey") == {"approval_id": "a1", "created_at": "1"}


def test_export_bad_cursor_400(monkeypatch):
    monkeypatch.setenv("APPROVALS_TABLE", "t")
    fake = MagicMock()
    with patch.object(handler.boto3, "resource") as res:
        res.return_value.Table.return_value = fake
        r = handler.lambda_handler(_activity_event({"cursor": "!!!not-base64!!!"}), None)
    assert r["statusCode"] == 400
    assert "cursor" in json.loads(r["body"]).get("error", "")
    fake.scan.assert_not_called()


def test_default_mode_unchanged(monkeypatch):
    # No export/cursor → still uses _scan_all + sort + truncate, shape {items,count}.
    monkeypatch.setenv("APPROVALS_TABLE", "t")
    rows = [{"approval_id": f"a{i}", "created_at": str(i), "approval_status": "approved"} for i in range(3)]
    with patch.object(handler, "_scan_all", return_value=rows), \
         patch.object(handler.boto3, "resource"):
        r = handler.lambda_handler(_activity_event({"limit": "2"}), None)
    body = json.loads(r["body"])
    assert body["count"] == 2  # truncated, sorted desc
    assert "next_cursor" not in body or body["next_cursor"] is None
    assert body["items"][0]["created_at"] == "2"  # newest first
```

Run: `python -m pytest tests/unit/api/test_activity.py -q`
Expected: the new paginated tests FAIL (mode not implemented); existing tests still pass.

- [ ] **Step 3: Implement the paginated mode + factor the projection.** In `api/approvals/handler.py`, inside the `/activity` GET block: after the filters are built (the `filters`/`attr_values` lists exist), refactor the `compact` projection into a module-level helper and branch on mode. Replace the current `items = sorted(_scan_all(...))[:limit]` ... `compact` ... `return` tail with:

```python
        scan_kwargs: dict = {}
        if filters:
            scan_kwargs["FilterExpression"] = " AND ".join(filters)
            scan_kwargs["ExpressionAttributeValues"] = attr_values

        cursor_param = qsp.get("cursor")
        export_mode = qsp.get("export") == "true" or bool(cursor_param)

        if export_mode:
            page = max(1, min(int(qsp.get("limit", "500")), 1000))
            if cursor_param:
                try:
                    scan_kwargs["ExclusiveStartKey"] = json.loads(
                        base64.urlsafe_b64decode(cursor_param)
                    )
                except Exception:
                    return {"statusCode": 400, "headers": headers,
                            "body": json.dumps({"error": "invalid cursor"})}
            scan_kwargs["Limit"] = page
            resp = table.scan(**scan_kwargs)
            compact = _compact_activity(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            next_cursor = (
                base64.urlsafe_b64encode(json.dumps(lek, default=str).encode()).decode()
                if lek else None
            )
            return {"statusCode": 200, "headers": headers,
                    "body": json.dumps({"items": compact, "count": len(compact),
                                        "next_cursor": next_cursor}, default=str)}

        items = sorted(_scan_all(table, **scan_kwargs), key=_created_ms, reverse=True)[:limit]
        compact = _compact_activity(items)
        return {"statusCode": 200, "headers": headers,
                "body": json.dumps({"items": compact, "count": len(compact)}, default=str)}
```

And add the `_compact_activity` helper near `_scan_all` (move the existing per-row projection verbatim into it):

```python
def _compact_activity(items: list) -> list:
    """Project approval rows to the compact activity-feed shape (strip noisy
    fields; action_details kept as a 500-char head excerpt)."""
    compact = []
    for it in items:
        details = it.get("action_details") or it.get("parameters") or {}
        details_str = details if isinstance(details, str) else json.dumps(details, default=str)
        compact.append({
            "approval_id": it.get("approval_id"),
            "created_at": it.get("created_at"),
            "resolved_at": it.get("resolved_at"),
            "consumed_at": it.get("consumed_at"),
            "approval_status": it.get("approval_status"),
            "cluster_id": it.get("cluster_id"),
            "action_type": it.get("action_type") or it.get("tool_name"),
            "requested_by": it.get("requested_by"),
            "approved_by": it.get("approved_by"),
            "action_details_excerpt": details_str[:500],
        })
    return compact
```

Keep the `limit = max(1, min(int(qsp.get("limit", "200")), 500))` line for the default mode (it still bounds the non-export view). Note the export mode re-reads `limit` with a 500 default / 1000 max for page size — that's intentional and separate.

- [ ] **Step 4: Run tests.**

Run: `python -m pytest tests/unit/api/test_activity.py -q`
Expected: PASS (new paginated tests + existing default-mode tests).

- [ ] **Step 5: Run the broader approvals/activity suite (no regression).**

Run: `python -m pytest tests/unit/api -q`
Expected: PASS.

- [ ] **Step 6: Commit.**

```bash
git add api/approvals/handler.py tests/unit/api/test_activity.py
git commit -m "feat(audit-export): cursor pagination mode on GET /api/activity"
```

---

### Task 2: Frontend — export loop over all pages

**Files:**

- Modify: `frontend/src/lib/api-client.ts` (`fetchActivity` opts/return + new `fetchAllActivity`)
- Modify: `frontend/src/app/activity/page.tsx` (export button onClick)

**Interfaces:**

- Consumes: `GET /api/activity?export=true&cursor=…` (Task 1) returning `{items, count, next_cursor}`.
- Produces: `fetchAllActivity(opts) -> Promise<{items: ActivityItem[]; capped: boolean}>`.

- [ ] **Step 1: Extend `fetchActivity` + add `fetchAllActivity`.** In `frontend/src/lib/api-client.ts`, update the `fetchActivity` opts + return type and add the loop helper (place `fetchAllActivity` right after `fetchActivity`):

```typescript
export async function fetchActivity(opts?: {
  cluster_id?: string;
  actor?: string;
  action_type?: string;
  limit?: number;
  cursor?: string;
  exportMode?: boolean;
}): Promise<{
  items: ActivityItem[];
  count: number;
  next_cursor?: string | null;
}> {
  const params = new URLSearchParams();
  if (opts?.cluster_id) params.set("cluster_id", opts.cluster_id);
  if (opts?.actor) params.set("actor", opts.actor);
  if (opts?.action_type) params.set("action_type", opts.action_type);
  if (opts?.limit) params.set("limit", String(opts.limit));
  if (opts?.cursor) params.set("cursor", opts.cursor);
  if (opts?.exportMode) params.set("export", "true");
  const qs = params.toString();
  const url = await api(`/api/activity${qs ? `?${qs}` : ""}`);
  const res = await authedFetch(url);
  if (!res.ok) throw new Error(`활동 조회 실패 (상태 ${res.status})`);
  return res.json();
}

// Loop the cursor-paginated export until exhausted, accumulating all rows.
// Continues while next_cursor is non-null (a filtered DDB page can be empty
// yet still have more pages). A hard page ceiling bounds a runaway loop.
export async function fetchAllActivity(opts?: {
  cluster_id?: string;
  actor?: string;
  action_type?: string;
}): Promise<{ items: ActivityItem[]; capped: boolean }> {
  const MAX_PAGES = 200;
  const items: ActivityItem[] = [];
  let cursor: string | undefined = undefined;
  let pages = 0;
  for (;;) {
    const page = await fetchActivity({
      ...opts,
      exportMode: true,
      limit: 1000,
      cursor,
    });
    items.push(...page.items);
    pages += 1;
    const next = page.next_cursor;
    if (!next) return { items, capped: false };
    if (pages >= MAX_PAGES) return { items, capped: true };
    cursor = next;
  }
}
```

- [ ] **Step 2: Wire the export button** in `frontend/src/app/activity/page.tsx`. Read the current onClick (~line 245-280) first. Replace the single `fetchActivity({...filters, limit: 500})` + `capped = r.items.length >= 500` logic with `fetchAllActivity`, sort the accumulated rows by `created_at` desc client-side (the export endpoint returns unsorted pages), then build the CSV. Concretely:

```tsx
                onClick={async () => {
                  let rows = items;
                  let capped = false;
                  try {
                    const r = await fetchAllActivity({
                      cluster_id: clusterFilter || undefined,
                      actor: actorFilter || undefined,
                      action_type: actionFilter || undefined,
                    });
                    rows = [...r.items].sort((a, b) =>
                      String(b.created_at).localeCompare(String(a.created_at)),
                    );
                    capped = r.capped;
                  } catch {
                    // fall back to the already-loaded in-view rows
                  }
                  const csv = buildAuditCsv(rows);
                  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
                  const a = document.createElement("a");
                  a.href = URL.createObjectURL(blob);
                  a.download = `audit-${new Date().toISOString().slice(0, 10)}-${rows.length}rows${
                    capped ? "-capped" : ""
                  }.csv`;
                  a.click();
                  URL.revokeObjectURL(a.href);
                }}
```

Match the exact names of the filter state variables in the file (`clusterFilter`/`actorFilter`/`actionFilter` may differ — read the component and use whatever it actually uses; preserve the existing button JSX/styling and any `URL.revokeObjectURL`/cleanup the current code does). The created_at values are mixed-format strings (ms-epoch for agent requests, ISO for UI) — sorting by `String.localeCompare` desc is a best-effort order consistent with the existing client view; do NOT introduce a new date parser here.

- [ ] **Step 3: Build the frontend.**

Run: `cd frontend && npm run build`
Expected: build succeeds, no type errors.

- [ ] **Step 4: Commit.**

```bash
git add frontend/src/lib/api-client.ts frontend/src/app/activity/page.tsx
git commit -m "feat(audit-export): export loops all pages (unbounded), client sorts + builds CSV"
```

---

## Post-implementation (controller, after both tasks reviewed clean)

- Final whole-branch review (most capable model) over `git merge-base main HEAD..HEAD`.
- Deploy dev: `cdk deploy dbops-dev-agent` (approvals Lambda code), then frontend build → `aws s3 sync frontend/out/ s3://dbops-dev-frontend-123456789012 --delete --exclude config.json` → CloudFront invalidation `E1234567890ABC`.
- Live smoke (viewer e2e token): `GET /api/activity?export=true` → 200 with `next_cursor` key present (null or a token); `GET /api/activity?cursor=garbage` → 400; default `GET /api/activity` unchanged shape.
- Then `superpowers:finishing-a-development-branch`.
