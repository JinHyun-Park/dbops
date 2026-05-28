"use client";

import { fmtBytes, fmtDecimal, fmtDuration, fmtNumber } from "@/lib/format";

interface ReportRow {
  id: number;
  cluster_id: string;
  report_type: string;
  report_date: string;
  summary: string;
  created_at: string;
}

interface ReportDetail extends ReportRow {
  data?: string | object | null;
}

interface ReportViewerProps {
  reports: ReportRow[];
  selectedRow: ReportRow | null;
  detail: ReportDetail | null;
  detailLoading: boolean;
  onSelect: (report: ReportRow) => void;
}

interface SlowRow {
  query_hash?: string;
  query_excerpt?: string;
  calls?: number;
  total_ms?: number;
  mean_ms?: number;
}

interface AlertRow {
  rule_id?: string;
  fired_count?: number;
  last_fired?: string;
}

interface EventRow {
  event_type?: string;
  cnt?: number;
}

interface ReportPayload {
  cluster_id?: string;
  window_hours?: number;
  aas?: {
    avg_aas?: number;
    max_aas?: number;
    p95_aas?: number;
    samples?: number;
  };
  aas_peak?: { ts?: string; value?: number };
  aas_busy_minutes_above_threshold?: number;
  aas_busy_threshold?: number;
  top_slow_queries?: SlowRow[];
  top_alerts?: AlertRow[];
  storage?: { start_bytes?: number; end_bytes?: number; delta_bytes?: number };
  connections?: { max_conn?: number; avg_conn?: number };
  events_by_type?: EventRow[];
}

function parseData(
  raw: string | object | null | undefined,
): ReportPayload | null {
  if (!raw) return null;
  if (typeof raw === "string") {
    try {
      return JSON.parse(raw) as ReportPayload;
    } catch {
      return null;
    }
  }
  return raw as ReportPayload;
}

export function ReportViewer({
  reports,
  selectedRow,
  detail,
  detailLoading,
  onSelect,
}: ReportViewerProps) {
  const payload = parseData(detail?.data);

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)] gap-6">
      <aside className="bg-zinc-950 border border-zinc-800">
        <div className="px-4 py-3 border-b border-zinc-800 text-[11px] font-medium text-zinc-500">
          {reports.length} 개 리포트
        </div>
        <div className="divide-y divide-zinc-800 max-h-[70vh] overflow-y-auto">
          {reports.map((r) => {
            const active = selectedRow?.id === r.id;
            return (
              <button
                key={r.id}
                onClick={() => onSelect(r)}
                className={`w-full text-left px-4 py-3 transition-colors ${
                  active ? "bg-zinc-900" : "hover:bg-zinc-900/60"
                }`}
              >
                <div className="text-sm text-zinc-100">
                  {r.report_date} <span className="text-zinc-500">·</span>{" "}
                  <span className="text-zinc-500">{r.report_type}</span>
                </div>
                <div className="text-xs text-zinc-500 mt-1 truncate">
                  {r.cluster_id}
                </div>
              </button>
            );
          })}
        </div>
      </aside>

      <section className="bg-zinc-950 border border-zinc-800 p-6 min-h-[70vh]">
        {!selectedRow ? (
          <div className="h-full flex items-center justify-center text-sm text-zinc-500">
            왼쪽에서 리포트를 선택하세요
          </div>
        ) : detailLoading ? (
          <div className="text-sm text-zinc-500">불러오는 중…</div>
        ) : (
          <ReportDetailPanel row={detail || selectedRow} payload={payload} />
        )}
      </section>
    </div>
  );
}

function ReportDetailPanel({
  row,
  payload,
}: {
  row: ReportDetail | ReportRow;
  payload: ReportPayload | null;
}) {
  return (
    <div className="space-y-8">
      <header>
        <div className="text-[11px] font-medium text-zinc-500 mb-1">
          {row.report_type} report
        </div>
        <h2 className="text-2xl font-semibold tracking-tight text-zinc-50">
          {row.report_date} · {row.cluster_id}
        </h2>
        {row.summary && (
          <p className="mt-4 text-[15px] leading-relaxed text-zinc-200 max-w-3xl whitespace-pre-wrap">
            {row.summary}
          </p>
        )}
      </header>

      {payload && (
        <>
          <StatBlock payload={payload} />
          <SlowQueriesBlock rows={payload.top_slow_queries || []} />
          <AlertsBlock rows={payload.top_alerts || []} />
          <EventsBlock rows={payload.events_by_type || []} />
        </>
      )}
    </div>
  );
}

function StatBlock({ payload }: { payload: ReportPayload }) {
  const aas = payload.aas || {};
  const storage = payload.storage || {};
  const conns = payload.connections || {};
  const deltaBytes = Number(storage.delta_bytes ?? 0);
  return (
    <section>
      <div className="text-[11px] font-medium text-zinc-500 mb-3">
        24시간 요약
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-zinc-800 border border-zinc-800">
        <Cell
          label="AAS avg"
          value={fmtDecimal(aas.avg_aas, 2)}
          hint={`max ${fmtDecimal(aas.max_aas, 2)} · p95 ${fmtDecimal(
            aas.p95_aas,
            2,
          )}`}
        />
        <Cell
          label="피크 AAS"
          value={fmtDecimal(payload.aas_peak?.value, 2)}
          hint={
            payload.aas_peak?.ts
              ? new Date(payload.aas_peak.ts).toLocaleString()
              : "—"
          }
        />
        <Cell
          label={`AAS > ${payload.aas_busy_threshold ?? 5} 샘플`}
          value={fmtNumber(payload.aas_busy_minutes_above_threshold)}
          hint="1분 단위"
        />
        <Cell
          label="활성 연결"
          value={fmtNumber(conns.max_conn)}
          hint={`avg ${fmtDecimal(conns.avg_conn, 1)}`}
        />
        <Cell
          label="스토리지 변화"
          value={(deltaBytes >= 0 ? "+" : "") + fmtBytes(Math.abs(deltaBytes))}
          hint={`${fmtBytes(storage.start_bytes)} → ${fmtBytes(
            storage.end_bytes,
          )}`}
        />
        <Cell
          label="샘플 수"
          value={fmtNumber(aas.samples)}
          hint="AAS 메트릭"
        />
      </div>
    </section>
  );
}

function Cell({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="bg-zinc-950 px-4 py-4">
      <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-zinc-500 mb-1">
        {label}
      </div>
      <div className="text-xl font-semibold text-zinc-100 tabular-nums">
        {value}
      </div>
      {hint && (
        <div className="text-[11px] text-zinc-500 mt-1 truncate" title={hint}>
          {hint}
        </div>
      )}
    </div>
  );
}

function SlowQueriesBlock({ rows }: { rows: SlowRow[] }) {
  if (!rows.length) return null;
  return (
    <section>
      <div className="text-[11px] font-medium text-zinc-500 mb-3">
        Top slow queries
      </div>
      <div className="border border-zinc-800 divide-y divide-zinc-800">
        {rows.map((q, i) => (
          <div key={q.query_hash || i} className="px-4 py-3">
            <div className="flex items-baseline justify-between gap-4 mb-1">
              <div className="text-xs text-zinc-500 font-mono">
                {(q.query_hash || "").slice(0, 12)}…
              </div>
              <div className="text-xs text-zinc-400 tabular-nums">
                {fmtDuration(q.total_ms)} 누적 · {fmtNumber(q.calls)} calls ·
                mean {fmtDuration(q.mean_ms)}
              </div>
            </div>
            <pre className="text-xs text-zinc-300 font-mono whitespace-pre-wrap break-all">
              {(q.query_excerpt || "").trim()}
            </pre>
          </div>
        ))}
      </div>
    </section>
  );
}

function AlertsBlock({ rows }: { rows: AlertRow[] }) {
  if (!rows.length) return null;
  return (
    <section>
      <div className="text-[11px] font-medium text-zinc-500 mb-3">
        Top alert rules
      </div>
      <div className="border border-zinc-800 divide-y divide-zinc-800">
        {rows.map((a, i) => (
          <div
            key={(a.rule_id || "") + i}
            className="px-4 py-3 flex items-baseline justify-between"
          >
            <div className="text-sm text-zinc-200 font-mono">{a.rule_id}</div>
            <div className="text-xs text-zinc-500 tabular-nums">
              {fmtNumber(a.fired_count)}회 ·{" "}
              {a.last_fired ? new Date(a.last_fired).toLocaleString() : ""}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function EventsBlock({ rows }: { rows: EventRow[] }) {
  if (!rows.length) return null;
  return (
    <section>
      <div className="text-[11px] font-medium text-zinc-500 mb-3">
        이벤트 분포
      </div>
      <div className="flex flex-wrap gap-2">
        {rows.map((e, i) => (
          <div
            key={(e.event_type || "") + i}
            className="border border-zinc-800 px-3 py-1.5 text-xs"
          >
            <span className="text-zinc-300">{e.event_type}</span>{" "}
            <span className="text-zinc-500 tabular-nums">
              · {fmtNumber(e.cnt)}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
