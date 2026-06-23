import { fmtBytes } from "@/lib/format";

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

export interface ReportForDownload {
  cluster_id: string;
  report_date: string;
  report_type: string;
  summary?: string | null;
  data?: string | object | null;
}

function parsePayload(
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

function fmt(v: number | undefined, decimals = 0): string {
  if (v == null) return "—";
  return decimals > 0 ? v.toFixed(decimals) : String(v);
}

export function buildReportMarkdown(report: ReportForDownload): string {
  const { cluster_id, report_date, report_type, summary, data } = report;
  const lines: string[] = [];

  lines.push(`# DBOps 리포트 — ${cluster_id} (${report_date})`);
  lines.push("");
  lines.push(`**유형:** ${report_type}`);
  lines.push("");

  if (summary) {
    lines.push("## 요약");
    lines.push("");
    lines.push(summary);
    lines.push("");
  }

  const payload = parsePayload(data);

  if (payload) {
    const aas = payload.aas || {};
    const storage = payload.storage || {};
    const conns = payload.connections || {};
    const deltaBytes = Number(storage.delta_bytes ?? 0);

    lines.push("## 24시간 지표");
    lines.push("");
    lines.push("| 항목 | 값 |");
    lines.push("| --- | --- |");
    lines.push(`| AAS avg | ${fmt(aas.avg_aas, 2)} |`);
    lines.push(`| AAS max | ${fmt(aas.max_aas, 2)} |`);
    lines.push(`| AAS p95 | ${fmt(aas.p95_aas, 2)} |`);
    lines.push(
      `| 피크 AAS | ${fmt(payload.aas_peak?.value, 2)}${
        payload.aas_peak?.ts
          ? " (" + new Date(payload.aas_peak.ts).toLocaleString() + ")"
          : ""
      } |`,
    );
    lines.push(
      `| AAS > ${payload.aas_busy_threshold ?? 5} 샘플 | ${fmt(
        payload.aas_busy_minutes_above_threshold,
      )} 분 |`,
    );
    lines.push(`| 활성 연결 max | ${fmt(conns.max_conn)} |`);
    lines.push(`| 활성 연결 avg | ${fmt(conns.avg_conn, 1)} |`);
    lines.push(
      `| 스토리지 변화 | ${
        (deltaBytes >= 0 ? "+" : "") + fmtBytes(deltaBytes)
      } |`,
    );
    lines.push(`| 스토리지 시작 | ${fmtBytes(storage.start_bytes)} |`);
    lines.push(`| 스토리지 종료 | ${fmtBytes(storage.end_bytes)} |`);
    lines.push(`| AAS 샘플 수 | ${fmt(aas.samples)} |`);
    lines.push("");

    const slowQueries = payload.top_slow_queries || [];
    if (slowQueries.length > 0) {
      lines.push("## Top slow queries");
      lines.push("");
      lines.push("| query_hash | calls | total_ms | mean_ms | excerpt |");
      lines.push("| --- | --- | --- | --- | --- |");
      for (const q of slowQueries) {
        const hash = (q.query_hash || "").slice(0, 12);
        const excerpt = (q.query_excerpt || "")
          .trim()
          .replace(/\n/g, " ")
          .slice(0, 80);
        lines.push(
          `| ${hash} | ${fmt(q.calls)} | ${fmt(q.total_ms)} | ${fmt(
            q.mean_ms,
          )} | ${excerpt} |`,
        );
      }
      lines.push("");
    }

    const alerts = payload.top_alerts || [];
    if (alerts.length > 0) {
      lines.push("## Top alert rules");
      lines.push("");
      lines.push("| rule_id | fired_count | last_fired |");
      lines.push("| --- | --- | --- |");
      for (const a of alerts) {
        const lastFired = a.last_fired
          ? new Date(a.last_fired).toLocaleString()
          : "—";
        lines.push(
          `| ${a.rule_id ?? "—"} | ${fmt(a.fired_count)} | ${lastFired} |`,
        );
      }
      lines.push("");
    }

    const events = payload.events_by_type || [];
    if (events.length > 0) {
      lines.push("## 이벤트 분포");
      lines.push("");
      lines.push("| event_type | count |");
      lines.push("| --- | --- |");
      for (const e of events) {
        lines.push(`| ${e.event_type ?? "—"} | ${fmt(e.cnt)} |`);
      }
      lines.push("");
    }
  }

  return lines.join("\n");
}
