"use client";

import { useState } from "react";
import { fmtBytes, fmtDecimal, fmtDuration, fmtNumber } from "@/lib/format";
import { buildReportMarkdown } from "@/lib/report-download";
import { apiUrl, authedFetch } from "@/lib/api-client";

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
  s3_key?: string;
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

interface FleetClusterRow {
  cluster_id?: string;
  engine?: string;
  health?: string;
  aas_avg?: number;
  aas_max?: number;
  slow_query_count?: number;
  alert_count?: number;
  storage_delta_bytes?: number;
}

interface FleetPayload {
  clusters_total?: number;
  engine_counts?: Record<string, number>;
  health_distribution?: Record<string, number>;
  totals?: { alerts?: number; slow_queries?: number };
  worst_clusters?: FleetClusterRow[];
  clusters?: FleetClusterRow[];
}

const FLEET_ID = "*";

function clusterLabel(clusterId: string): string {
  return clusterId === FLEET_ID ? "Fleet 전체" : clusterId;
}

function parseJson<T>(raw: string | object | null | undefined): T | null {
  if (!raw) return null;
  if (typeof raw === "string") {
    try {
      return JSON.parse(raw) as T;
    } catch {
      return null;
    }
  }
  return raw as T;
}

function parseData(
  raw: string | object | null | undefined,
): ReportPayload | null {
  return parseJson<ReportPayload>(raw);
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
                  {r.cluster_id === FLEET_ID ? (
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-indigo-500/15 text-indigo-300 border border-indigo-500/30 text-[10px] font-medium tracking-wide">
                      Fleet 전체
                    </span>
                  ) : (
                    r.cluster_id
                  )}
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
          <ReportDetailPanel
            key={detail?.id ?? selectedRow?.id}
            row={detail || selectedRow}
            payload={payload}
            detail={detail}
          />
        )}
      </section>
    </div>
  );
}

function ReportDetailPanel({
  row,
  payload,
  detail,
}: {
  row: ReportDetail | ReportRow;
  payload: ReportPayload | null;
  detail: ReportDetail | null;
}) {
  const [htmlLoading, setHtmlLoading] = useState(false);
  const [htmlUnavailable, setHtmlUnavailable] = useState(false);
  const isFleet = row.cluster_id === FLEET_ID;
  const fleetPayload = isFleet ? parseJson<FleetPayload>(detail?.data) : null;

  function handleDownload() {
    if (!detail) return;
    const md = buildReportMarkdown({
      cluster_id: detail.cluster_id,
      report_date: detail.report_date,
      report_type: detail.report_type,
      summary: detail.summary,
      data: detail.data,
    });
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `report-${detail.cluster_id}-${detail.report_date}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function handleHtmlDownload() {
    if (!detail || htmlLoading || htmlUnavailable) return;
    setHtmlLoading(true);
    try {
      const url = await apiUrl(`/api/reports/${detail.id}/html`);
      const res = await authedFetch(url);
      if (res.status === 404) {
        setHtmlUnavailable(true);
        return;
      }
      if (!res.ok) {
        console.error("[reports] html presign failed", res.status);
        return;
      }
      const { url: presignedUrl } = await res.json();
      window.open(presignedUrl, "_blank", "noopener,noreferrer");
    } catch (e) {
      console.error("[reports] html download error", e);
    } finally {
      setHtmlLoading(false);
    }
  }

  return (
    <div className="space-y-8">
      <header>
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-[11px] font-medium text-zinc-500 mb-1">
              {row.report_type} report
            </div>
            <h2 className="text-2xl font-semibold tracking-tight text-zinc-50">
              {row.report_date} · {clusterLabel(row.cluster_id)}
            </h2>
          </div>
          {detail && (
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={handleDownload}
                className="px-3 py-1.5 text-xs font-medium border border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
              >
                다운로드
              </button>
              <button
                onClick={handleHtmlDownload}
                disabled={htmlLoading || htmlUnavailable}
                title={
                  htmlUnavailable
                    ? "이 리포트는 HTML 미생성"
                    : "HTML 파일을 새 탭에서 엽니다"
                }
                className={`px-3 py-1.5 text-xs font-medium border transition-colors ${
                  htmlUnavailable
                    ? "border-zinc-800 text-zinc-600 cursor-not-allowed"
                    : htmlLoading
                      ? "border-zinc-700 text-zinc-500 cursor-wait"
                      : "border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100"
                }`}
              >
                {htmlLoading ? "…" : "HTML 다운로드"}
              </button>
            </div>
          )}
        </div>
        {row.summary && (
          <p className="mt-4 text-[15px] leading-relaxed text-zinc-200 max-w-3xl whitespace-pre-wrap">
            {row.summary}
          </p>
        )}
      </header>

      {isFleet
        ? fleetPayload && <FleetDetailPanel payload={fleetPayload} />
        : payload && (
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

function FleetDetailPanel({ payload }: { payload: FleetPayload }) {
  const totals = payload.totals || {};
  const worst = payload.worst_clusters || [];
  const clusters = payload.clusters || [];
  const engineCounts = Object.entries(payload.engine_counts || {});
  const healthDist = Object.entries(payload.health_distribution || {});

  return (
    <div className="space-y-8">
      <section>
        <div className="text-[11px] font-medium text-zinc-500 mb-3">
          Fleet 요약
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-px bg-zinc-800 border border-zinc-800">
          <Cell label="클러스터 수" value={fmtNumber(payload.clusters_total)} />
          <Cell label="총 경보" value={fmtNumber(totals.alerts)} />
          <Cell label="총 슬로우 쿼리" value={fmtNumber(totals.slow_queries)} />
        </div>
      </section>

      {(engineCounts.length > 0 || healthDist.length > 0) && (
        <section className="flex flex-wrap gap-2">
          {engineCounts.map(([engine, n]) => (
            <span
              key={`e-${engine}`}
              className="border border-zinc-800 px-3 py-1.5 text-xs text-zinc-300"
            >
              {engine}{" "}
              <span className="text-zinc-500 tabular-nums">
                · {fmtNumber(n)}
              </span>
            </span>
          ))}
          {healthDist.map(([bucket, n]) => (
            <span
              key={`h-${bucket}`}
              className="border border-zinc-800 px-3 py-1.5 text-xs text-zinc-300"
            >
              {bucket}{" "}
              <span className="text-zinc-500 tabular-nums">
                · {fmtNumber(n)}
              </span>
            </span>
          ))}
        </section>
      )}

      {worst.length > 0 && (
        <FleetClusterTable
          title="주의가 필요한 클러스터 (Top 5)"
          rows={worst}
        />
      )}
      {clusters.length > 0 && (
        <FleetClusterTable title="전체 클러스터" rows={clusters} />
      )}
    </div>
  );
}

function FleetClusterTable({
  title,
  rows,
}: {
  title: string;
  rows: FleetClusterRow[];
}) {
  return (
    <section>
      <div className="text-[11px] font-medium text-zinc-500 mb-3">{title}</div>
      <div className="border border-zinc-800 overflow-x-auto">
        <table className="w-full text-xs text-zinc-300">
          <thead>
            <tr className="text-zinc-500 border-b border-zinc-800">
              <th className="text-left font-medium px-3 py-2">클러스터</th>
              <th className="text-left font-medium px-3 py-2">엔진</th>
              <th className="text-left font-medium px-3 py-2">상태</th>
              <th className="text-right font-medium px-3 py-2">AAS avg</th>
              <th className="text-right font-medium px-3 py-2">AAS max</th>
              <th className="text-right font-medium px-3 py-2">경보</th>
              <th className="text-right font-medium px-3 py-2">슬로우</th>
              <th className="text-right font-medium px-3 py-2">스토리지 Δ</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800">
            {rows.map((r, i) => {
              const delta = Number(r.storage_delta_bytes ?? 0);
              return (
                <tr key={(r.cluster_id || "") + i}>
                  <td className="px-3 py-2 font-mono text-zinc-200">
                    {r.cluster_id}
                  </td>
                  <td className="px-3 py-2">{r.engine}</td>
                  <td className="px-3 py-2">{r.health}</td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {fmtDecimal(r.aas_avg, 2)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {fmtDecimal(r.aas_max, 2)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {fmtNumber(r.alert_count)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {fmtNumber(r.slow_query_count)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {(delta >= 0 ? "+" : "") + fmtBytes(Math.abs(delta))}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
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
