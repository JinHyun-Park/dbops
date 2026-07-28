"use client";

import { useEffect, useState } from "react";
import {
  fetchSchemaChanges,
  type SchemaChangeRow,
  type SchemaChangesResponse,
} from "@/lib/api-client";
import { fmtExact, fmtNumber } from "@/lib/format";

/** null means UNKNOWN. The row count for a created/dropped table comes from
 * table_stats, which only records the 100 largest tables, so it is absent for
 * anything smaller. Coercing that to 0 told the DBA a table had zero rows. */
function num(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const x = Number(v);
  return Number.isFinite(x) ? x : null;
}

const UNKNOWN_ROWS = "행 수 미상";

const TYPE_STYLES: Record<string, { color: string; bg: string; icon: string }> =
  {
    created: { color: "text-emerald-400", bg: "bg-emerald-500/10", icon: "＋" },
    dropped: { color: "text-rose-400", bg: "bg-rose-500/10", icon: "－" },
    changed: { color: "text-amber-400", bg: "bg-amber-500/10", icon: "Δ" },
    renamed: { color: "text-purple-300", bg: "bg-purple-500/10", icon: "↻" },
  };

// Each chip states what a SOURCE could measure. "ok" is the only state that
// licenses reading an empty list as "nothing changed".
const DDL_CHIP: Record<string, { label: string; ok: boolean }> = {
  ok: { label: "DDL 판정됨", ok: true },
  not_collected: { label: "DDL 미수집", ok: false },
  baseline_only: { label: "DDL baseline만", ok: false },
  outside_window: { label: "DDL 구간 밖", ok: false },
  unavailable: { label: "DDL 조회 불가", ok: false },
};

const ROWS_CHIP: Record<string, { label: string; ok: boolean }> = {
  ok: { label: "행 수 비교됨", ok: true },
  insufficient_history: { label: "행 수 이력 부족", ok: false },
  no_data: { label: "행 수 미수집", ok: false },
};

const COLLECTION_CHIP: Record<string, { label: string; ok: boolean }> = {
  fresh: { label: "수집 최신", ok: true },
  stale: { label: "수집 지연", ok: false },
  no_data: { label: "수집 기록 없음", ok: false },
};

// The empty list means something different in each of these states, and only
// the first one is an absence of change.
const EMPTY_STATE: Record<string, { title: string; tone: string }> = {
  no_changes: { title: "이 구간에서 감지된 변경 없음", tone: "text-zinc-400" },
  not_collected: {
    title: "수집 이력이 없어 변경 여부를 판정할 수 없음",
    tone: "text-amber-300",
  },
  insufficient_history: {
    title: "비교 가능한 이력이 부족해 변경 여부를 판정할 수 없음",
    tone: "text-amber-300",
  },
  ok: { title: "이 구간에서 감지된 변경 없음", tone: "text-zinc-400" },
};

function Chip({ label, ok }: { label: string; ok: boolean }) {
  return (
    <span
      className={`px-1.5 py-0.5 rounded text-[10px] border ${
        ok
          ? "border-zinc-700 text-zinc-400"
          : "border-amber-500/40 bg-amber-500/10 text-amber-300"
      }`}
    >
      {label}
    </span>
  );
}

function RowCount({
  value,
  suffix,
  color,
}: {
  value: number | null;
  suffix: string;
  color: string;
}) {
  if (value === null) {
    return (
      <span
        className="text-zinc-500"
        title="table_stats는 매 주기 상위 100개 테이블만 기록하므로 이 테이블의 행 수는 수집되지 않았습니다"
      >
        {UNKNOWN_ROWS}
      </span>
    );
  }
  return (
    <span className={color} title={`${fmtExact(value)}${suffix}`}>
      {fmtNumber(value)}
      {suffix}
    </span>
  );
}

function ChangeRow({ c }: { c: SchemaChangeRow }) {
  const style = TYPE_STYLES[c.change_type] || TYPE_STYLES.changed;
  const baseline = num(c.baseline_rows);
  const current = num(c.current_rows);
  const delta =
    baseline !== null && current !== null ? current - baseline : null;
  return (
    <div className={`px-4 py-2.5 ${style.bg}`}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <span className={`text-lg font-mono ${style.color}`}>
            {style.icon}
          </span>
          <span
            className={`text-[10px] uppercase ${style.color} font-medium shrink-0`}
          >
            {c.change_type}
          </span>
          <span className="text-sm font-mono text-zinc-200 truncate">
            <span className="text-zinc-500">{c.schema_name}.</span>
            {c.table_name}
          </span>
        </div>
        <div className="text-xs font-mono text-zinc-400 tabular-nums shrink-0">
          {c.change_type === "created" && (
            <RowCount value={current} suffix=" 행" color="text-emerald-400" />
          )}
          {c.change_type === "dropped" && (
            <RowCount
              value={baseline}
              suffix=" 행 손실"
              color="text-rose-400"
            />
          )}
          {c.change_type === "changed" && (
            <>
              <RowCount value={baseline} suffix="" color="text-zinc-300" />
              <span className="text-zinc-600 mx-1.5">→</span>
              <RowCount value={current} suffix="" color="text-zinc-300" />
              {delta !== null && baseline !== null && (
                <span
                  className={`ml-2 ${
                    delta > 0 ? "text-emerald-400" : "text-rose-400"
                  }`}
                  title={`증감 ${delta > 0 ? "+" : ""}${fmtExact(delta)}`}
                >
                  ({delta > 0 ? "+" : ""}
                  {baseline > 0 ? ((delta / baseline) * 100).toFixed(0) : "∞"}%)
                </span>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export function SchemaChangesPanel({ clusterId }: { clusterId: string }) {
  const [data, setData] = useState<SchemaChangesResponse | null>(null);
  const [error, setError] = useState(false);
  const [days, setDays] = useState(7);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    fetchSchemaChanges(clusterId, days)
      .then((d) => !cancelled && setData(d))
      .catch(() => {
        if (cancelled) return;
        // A failed fetch is not an absence of changes.
        setData(null);
        setError(true);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [clusterId, days]);

  const changes = data?.changes ?? [];
  const ddl = data?.ddl_detection;
  const renames = ddl?.rename_candidates ?? [];
  const shown = changes.length + renames.length;
  const empty = EMPTY_STATE[data?.status ?? "no_changes"] ?? EMPTY_STATE.ok;
  // The frontend is a static export deployed separately from the API Lambda, so
  // it can meet a payload predating these fields. Unknown, never "ok".
  const coll = data?.collection;
  const rowStatus = data?.row_deltas?.status ?? "no_data";
  const age = coll?.age_hours;

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-zinc-800 flex items-start justify-between gap-3">
        <div>
          <div className="text-sm text-zinc-200 font-medium">
            Schema Changes
            {shown > 0 && (
              <span className="ml-2 px-1.5 py-0.5 bg-amber-500/20 text-amber-300 rounded text-[10px]">
                {shown}
              </span>
            )}
          </div>
          <div className="text-[11px] text-zinc-500 mt-0.5">
            테이블 생성·삭제·이름변경 또는 행 수가 크게 변한 항목
          </div>
        </div>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="bg-zinc-900 border border-zinc-800 text-zinc-200 text-xs rounded px-2 py-1"
        >
          <option value={1}>최근 1일</option>
          <option value={7}>최근 7일</option>
          <option value={30}>최근 30일</option>
        </select>
      </div>

      {loading ? (
        <div className="p-6 text-zinc-500 text-sm">불러오는 중…</div>
      ) : error || !data ? (
        <div className="p-6 text-sm">
          <div className="text-amber-300">스키마 변경 조회 실패</div>
          <div className="text-[11px] text-zinc-500 mt-1">
            변경이 없다는 뜻이 아닙니다. 잠시 후 다시 시도하세요.
          </div>
        </div>
      ) : (
        <>
          {shown === 0 ? (
            <div className="p-6 text-sm">
              <div className={empty.tone}>{empty.title}</div>
              {data.status === "no_changes" && (
                <div className="text-[11px] text-zinc-500 mt-1 tabular-nums">
                  DDL 비교 {ddl?.schemas_compared ?? 0}개 schema · 행 수 비교{" "}
                  {data.row_deltas?.tables_compared ?? 0}개 table
                </div>
              )}
            </div>
          ) : (
            <div className="divide-y divide-zinc-700">
              {changes.map((c, i) => (
                <ChangeRow
                  key={`${c.schema_name}-${c.table_name}-${i}`}
                  c={c}
                />
              ))}
              {renames.map((r, i) => (
                <div
                  key={`ren-${r.schema_name}-${r.from}-${i}`}
                  className={`px-4 py-2.5 ${TYPE_STYLES.renamed.bg}`}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <span
                      className={`text-lg font-mono ${TYPE_STYLES.renamed.color}`}
                    >
                      {TYPE_STYLES.renamed.icon}
                    </span>
                    <span
                      className={`text-[10px] uppercase ${TYPE_STYLES.renamed.color} font-medium shrink-0`}
                    >
                      renamed?
                    </span>
                    <span className="text-sm font-mono text-zinc-200 truncate">
                      <span className="text-zinc-500">{r.schema_name}.</span>
                      {r.from}
                      <span className="text-zinc-600 mx-1.5">→</span>
                      {r.to}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}

          {data.truncated && (
            <div className="px-4 py-2 border-t border-zinc-800 text-[11px] text-zinc-500 tabular-nums">
              전체 {data.total_changes}건 중 {changes.length}건만 표시
            </div>
          )}

          <div className="px-4 py-2.5 border-t border-zinc-800 flex flex-wrap items-center gap-1.5">
            {/* A missing field falls back to the UNKNOWN chip, never to "ok". */}
            <Chip
              {...(DDL_CHIP[ddl?.status ?? "unavailable"] ??
                DDL_CHIP.unavailable)}
            />
            <Chip {...(ROWS_CHIP[rowStatus] ?? ROWS_CHIP.no_data)} />
            <Chip
              {...(COLLECTION_CHIP[coll?.status ?? "no_data"] ??
                COLLECTION_CHIP.no_data)}
              label={
                (
                  COLLECTION_CHIP[coll?.status ?? "no_data"] ??
                  COLLECTION_CHIP.no_data
                ).label +
                (coll?.status === "stale" && age != null ? ` ${age}h` : "")
              }
            />
          </div>

          {data.note && (
            <div className="px-4 py-2.5 border-t border-zinc-800 text-[11px] leading-relaxed text-zinc-400">
              {data.note}
            </div>
          )}
        </>
      )}
    </div>
  );
}
