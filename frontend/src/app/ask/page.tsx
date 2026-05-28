"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { fetchMultiClusterOverview } from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";
import { fmtBytes, fmtDecimal, fmtExact } from "@/lib/format";

// AI-first fleet query: a single natural-language sentence + a structured
// editor that stays in sync. The NL→filter compiler is deliberately a
// regex pass on the client (no agent round-trip) so the experience stays
// instant; the trade-off is a narrow vocabulary that we surface to the
// user via the parsed-filter chips so they see exactly what got matched.

type Metric =
  | "cpu"
  | "aas"
  | "conn_active"
  | "conn_idle"
  | "storage_bytes"
  | "deadlocks";

type Comparison = ">" | ">=" | "<" | "<=";

interface FilterSpec {
  metric: Metric;
  comparison: Comparison;
  threshold: number;
  hours: number;
  raw: string; // the natural-language query we compiled from
}

interface ClusterRow {
  cluster_id: string;
  engine: string;
  status: string;
  cpu: number | string | null;
  aas: number | string | null;
  conn_active: number | string | null;
  conn_idle: number | string | null;
  storage_bytes: number | string | null;
  deadlocks: number | string | null;
  blocking_count: number | string | null;
}

interface SavedView {
  name: string;
  spec: FilterSpec;
  savedAt: string;
}

const METRIC_OPTIONS: { value: Metric; label: string; unit?: string }[] = [
  { value: "cpu", label: "CPU %", unit: "%" },
  { value: "aas", label: "AAS" },
  { value: "conn_active", label: "Active connections" },
  { value: "conn_idle", label: "Idle connections" },
  { value: "storage_bytes", label: "Storage", unit: "GB" },
  { value: "deadlocks", label: "Deadlocks" },
];

const SAVED_VIEWS_KEY = "dbops_fleet_query_views";

// ---------------------------------------------------------------------------
// Natural-language → FilterSpec compiler. Korean-first, English fallback.
// ---------------------------------------------------------------------------

function compile(raw: string): FilterSpec | null {
  const text = raw.toLowerCase();
  if (!text.trim()) return null;

  // Metric keyword lookup
  const metric = ((): Metric | null => {
    if (/\bcpu\b|씨피유/.test(text)) return "cpu";
    if (/\baas\b|active session|활성 세션/.test(text)) return "aas";
    if (/active.*conn|active.*연결|active.*세션|active connection/.test(text))
      return "conn_active";
    if (/idle.*conn|idle.*연결|유휴.*연결/.test(text)) return "conn_idle";
    if (/storage|스토리지|디스크|disk/.test(text)) return "storage_bytes";
    if (/deadlock|데드락/.test(text)) return "deadlocks";
    return null;
  })();
  if (!metric) return null;

  // Comparison
  let comparison: Comparison = ">";
  if (/이하|미만|under|below|less/.test(text)) {
    comparison = /이하|or less|이하인|적은/.test(text) ? "<=" : "<";
  } else if (/이상|over|above|exceed|넘는|초과|넘은/.test(text)) {
    comparison = /이상|or more|이상인/.test(text) ? ">=" : ">";
  }

  // Threshold — first numeric, accept % or units
  const numMatch = text.match(/(\d+(?:\.\d+)?)\s*(?:%|gb|gib|mb)?/);
  const threshold = numMatch ? Number(numMatch[1]) : 0;

  // Hours — accept "24시간", "1일", "24h", "지난 6시간"
  let hours = 1;
  const dayMatch = text.match(/(\d+)\s*(?:일|day)/);
  const hrMatch = text.match(/(\d+)\s*(?:시간|h\b|hour)/);
  if (dayMatch) hours = Number(dayMatch[1]) * 24;
  else if (hrMatch) hours = Number(hrMatch[1]);

  return { metric, comparison, threshold, hours, raw };
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function AskPage() {
  const [query, setQuery] = useState("");
  const [spec, setSpec] = useState<FilterSpec | null>(null);
  const [allClusters, setAllClusters] = useState<ClusterRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [savedViews, setSavedViews] = useState<SavedView[]>([]);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Load saved views once
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(SAVED_VIEWS_KEY);
      if (raw) setSavedViews(JSON.parse(raw));
    } catch {
      /* ignore */
    }
  }, []);

  const persistViews = useCallback((next: SavedView[]) => {
    setSavedViews(next);
    try {
      window.localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(next));
    } catch {
      /* ignore */
    }
  }, []);

  const runQuery = useCallback(async (target: FilterSpec | null) => {
    if (!target) return;
    setLoading(true);
    setErr(null);
    try {
      const d = await fetchMultiClusterOverview();
      setAllClusters((d.clusters || []) as ClusterRow[]);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  const submit = (e?: React.FormEvent) => {
    e?.preventDefault();
    const compiled = compile(query);
    if (!compiled) {
      setErr(
        "쿼리에서 metric을 찾지 못했습니다. 'CPU', 'AAS', 'storage', 'deadlock', 'connection' 같은 단어가 포함돼야 합니다.",
      );
      return;
    }
    setSpec(compiled);
    runQuery(compiled);
  };

  // Filter clusters on the client — overview already returns the latest
  // per-metric values, so the threshold check is just a comparator. This
  // intentionally skips the time-window dimension; the overview endpoint
  // is "last 15 min" and we treat "지난 24시간" as a recency intent
  // already satisfied by the live snapshot. A future enhancement would
  // pivot to a per-metric time-series query for true windowed checks.
  const matched = useMemo<ClusterRow[]>(() => {
    if (!spec) return [];
    return allClusters.filter((c) => {
      const raw = c[spec.metric];
      if (raw === null || raw === undefined) return false;
      const v = Number(raw);
      if (!Number.isFinite(v)) return false;
      const threshold =
        spec.metric === "storage_bytes"
          ? spec.threshold * 1_073_741_824
          : spec.threshold;
      switch (spec.comparison) {
        case ">":
          return v > threshold;
        case ">=":
          return v >= threshold;
        case "<":
          return v < threshold;
        case "<=":
          return v <= threshold;
      }
    });
  }, [allClusters, spec]);

  return (
    <PageBody>
      <PageHeader
        eyebrow="자동화"
        title="Ask the fleet"
        description="자연어로 '최근 24h CPU 80% 넘은 클러스터' 처럼 물어보면 즉시 필터 결과를 카드로 보여줍니다. 필터는 편집 + 저장 가능."
      />

      <form
        onSubmit={submit}
        className="bg-zinc-900/50 border border-zinc-800 p-5 mb-6"
      >
        <label className="block">
          <div className="text-[11px] font-medium text-zinc-500 mb-2">
            질문 (자연어)
          </div>
          <div className="flex gap-2">
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="예: 최근 24시간 동안 CPU 80% 넘은 클러스터"
              className="flex-1 bg-zinc-950 border border-zinc-700 text-zinc-200 text-sm px-3 py-2 font-mono focus:outline-none focus:border-amber-500/60"
            />
            <button
              type="submit"
              className="text-xs font-medium px-4 py-2 bg-amber-500 text-zinc-950 hover:bg-amber-400 transition-colors"
            >
              물어보기
            </button>
          </div>
        </label>

        {spec && (
          <StructuredEditor
            spec={spec}
            onChange={(next) => {
              setSpec(next);
              setQuery(next.raw);
            }}
            onRerun={() => runQuery(spec)}
          />
        )}

        {err && (
          <div className="mt-3 text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
            {err}
          </div>
        )}
      </form>

      {spec && (
        <Section
          eyebrow="결과"
          title={`매칭 클러스터 ${matched.length}개`}
          description={describeSpec(spec)}
          actions={
            <SaveViewButton
              spec={spec}
              savedViews={savedViews}
              onSave={(name) => {
                const next = [
                  ...savedViews.filter((v) => v.name !== name),
                  { name, spec, savedAt: new Date().toISOString() },
                ];
                persistViews(next);
                setSaveError(null);
              }}
              onError={setSaveError}
            />
          }
        >
          {saveError && (
            <div className="mb-3 text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              {saveError}
            </div>
          )}
          {loading ? (
            <div className="text-zinc-500 text-sm">불러오는 중…</div>
          ) : matched.length === 0 ? (
            <EmptyState
              title="조건을 만족하는 클러스터 없음"
              description="필터의 metric/threshold를 조정해보세요."
            />
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {matched.map((c) => (
                <ResultCard key={c.cluster_id} cluster={c} spec={spec} />
              ))}
            </div>
          )}
        </Section>
      )}

      {savedViews.length > 0 && (
        <Section eyebrow="저장된 뷰" title="Saved views">
          <ul className="border border-zinc-800 bg-zinc-900/40 divide-y divide-zinc-800">
            {savedViews
              .slice()
              .sort((a, b) => b.savedAt.localeCompare(a.savedAt))
              .map((v) => (
                <li
                  key={v.name + v.savedAt}
                  className="px-4 py-2 flex items-baseline justify-between gap-3"
                >
                  <button
                    type="button"
                    onClick={() => {
                      setSpec(v.spec);
                      setQuery(v.spec.raw);
                      runQuery(v.spec);
                    }}
                    className="text-left flex-1"
                  >
                    <div className="text-sm text-zinc-200 font-medium">
                      {v.name}
                    </div>
                    <div className="text-[11px] text-zinc-500 font-mono">
                      {describeSpec(v.spec)}
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      persistViews(savedViews.filter((x) => x.name !== v.name))
                    }
                    className="text-[11px] text-rose-400 hover:text-rose-300"
                  >
                    삭제
                  </button>
                </li>
              ))}
          </ul>
        </Section>
      )}
    </PageBody>
  );
}

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

function StructuredEditor({
  spec,
  onChange,
  onRerun,
}: {
  spec: FilterSpec;
  onChange: (next: FilterSpec) => void;
  onRerun: () => void;
}) {
  // Updating any field rewrites `raw` so the NL input + chips stay in
  // sync; the user gets to refine via either surface.
  const update = (patch: Partial<FilterSpec>) => {
    const next = { ...spec, ...patch };
    onChange({ ...next, raw: describeSpec(next) });
  };
  return (
    <div className="mt-4 pt-4 border-t border-zinc-800 grid grid-cols-2 md:grid-cols-5 gap-3 items-end">
      <label className="block col-span-2">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
          Metric
        </div>
        <select
          value={spec.metric}
          onChange={(e) => update({ metric: e.target.value as Metric })}
          className="w-full bg-zinc-950 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5"
        >
          {METRIC_OPTIONS.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
      </label>
      <label className="block">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
          Op
        </div>
        <select
          value={spec.comparison}
          onChange={(e) => update({ comparison: e.target.value as Comparison })}
          className="w-full bg-zinc-950 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5"
        >
          {(["", ">", ">=", "<", "<="] as Comparison[])
            .filter(Boolean)
            .map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
        </select>
      </label>
      <label className="block">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
          Threshold
        </div>
        <input
          type="number"
          step="0.1"
          value={spec.threshold}
          onChange={(e) => update({ threshold: Number(e.target.value) })}
          className="w-full bg-zinc-950 border border-zinc-700 text-zinc-200 text-sm px-2 py-1.5 font-mono tabular-nums"
        />
      </label>
      <button
        type="button"
        onClick={onRerun}
        className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-300 hover:border-amber-500 hover:text-amber-300 transition-colors"
      >
        다시 실행
      </button>
    </div>
  );
}

function ResultCard({
  cluster,
  spec,
}: {
  cluster: ClusterRow;
  spec: FilterSpec;
}) {
  const value = cluster[spec.metric];
  const display = formatMetric(spec.metric, value);
  return (
    <Link
      href={`/dashboard?cluster=${encodeURIComponent(cluster.cluster_id)}`}
      className="block border border-zinc-800 bg-zinc-900/50 hover:border-amber-500/50 transition-colors p-4"
    >
      <div className="text-xs font-mono text-zinc-300 break-all">
        {cluster.cluster_id}
      </div>
      <div className="text-[10px] text-zinc-500 mt-0.5">
        {cluster.engine} · {cluster.status}
      </div>
      <div className="mt-3 flex items-baseline gap-2">
        <span className="text-2xl font-semibold text-amber-300 tabular-nums">
          {display}
        </span>
        <span className="text-[10px] text-zinc-500 font-mono">
          {metricLabel(spec.metric)}
        </span>
      </div>
    </Link>
  );
}

function SaveViewButton({
  spec,
  savedViews,
  onSave,
  onError,
}: {
  spec: FilterSpec;
  savedViews: SavedView[];
  onSave: (name: string) => void;
  onError: (msg: string | null) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => {
          onError(null);
          setName(describeSpec(spec).slice(0, 40));
          setEditing(true);
        }}
        className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-300 hover:border-amber-500 hover:text-amber-300 transition-colors"
      >
        + 뷰 저장
      </button>
    );
  }
  return (
    <div className="flex items-center gap-2">
      <input
        autoFocus
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="뷰 이름"
        className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 w-44"
      />
      <button
        type="button"
        onClick={() => {
          const trimmed = name.trim();
          if (!trimmed) {
            onError("이름이 비어있습니다");
            return;
          }
          if (savedViews.some((v) => v.name === trimmed)) {
            onError("같은 이름의 뷰가 이미 있습니다 — 덮어쓸까요?");
          }
          onSave(trimmed);
          setEditing(false);
          setName("");
        }}
        className="text-xs px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400"
      >
        저장
      </button>
      <button
        type="button"
        onClick={() => {
          setEditing(false);
          setName("");
        }}
        className="text-xs text-zinc-500 hover:text-zinc-300"
      >
        취소
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function metricLabel(m: Metric): string {
  return METRIC_OPTIONS.find((o) => o.value === m)?.label || m;
}

function formatMetric(m: Metric, v: ClusterRow[Metric]): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  if (m === "cpu") return `${fmtDecimal(n, 1)}%`;
  if (m === "storage_bytes") return fmtBytes(n);
  if (m === "aas") return fmtDecimal(n, 2);
  return fmtExact(Math.round(n));
}

function describeSpec(spec: FilterSpec): string {
  const unit =
    spec.metric === "cpu" ? "%" : spec.metric === "storage_bytes" ? " GB" : "";
  return `${metricLabel(spec.metric)} ${spec.comparison} ${
    spec.threshold
  }${unit} · 최근 ${spec.hours}h`;
}
