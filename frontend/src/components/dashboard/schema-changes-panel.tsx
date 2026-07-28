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
const UNKNOWN_TYPE = "알 수 없는 변경 유형";

// Every `change_type` api/dashboard/handler.py puts in `changes`. ChangeRow has
// one cell per entry plus a fallthrough, for the same reason EmptyVerdict has
// one: this is a static export deployed separately from the api Lambda, so it can
// meet a row whose type it has never heard of (compute_diff already computes a
// `modified` list this tier does not surface yet). Without the fallthrough such a
// row rendered its NAME and nothing else, which is pass 1's "a real change
// rendered as nothing" in the positive half.
const KNOWN_CHANGE = ["created", "dropped", "changed"];

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
  not_comparable: { label: "DDL 비교 불가", ok: false },
  baseline_only: { label: "DDL baseline만", ok: false },
  outside_window: { label: "DDL 구간 밖", ok: false },
  unavailable: { label: "DDL 조회 불가", ok: false },
  // A REFUSAL, not a failure: this cluster's engine has a privilege-filtered
  // catalog, so a REVOKE and a DROP are the same read and no snapshot is collected.
  // It is drawn like every other blindness, because for the operator it IS one.
  not_supported: { label: "DDL 판정 미지원 엔진", ok: false },
};

// THE FOURTH SOURCE, and the one this panel did not have at all. "did the schema
// change" and "is the schema still there" are different questions, and conflating
// them is the defect six passes over this surface kept relocating. `not_seen` is a
// first-class value here because it is the ACCEPTED COST: a genuine DROP SCHEMA is
// never drawn as a drop, it is drawn as "not confirmed", and the note carries the
// last confirmed time. Same values the agent's `observation.status` carries.
const OBSERVATION_CHIP: Record<string, { label: string; ok: boolean }> = {
  fresh: { label: "스키마 존재 확인됨", ok: true },
  not_seen: { label: "확인 안 된 스키마 있음", ok: false },
  unmigrated: { label: "스키마 관측 기록 없음", ok: false },
  no_snapshots: { label: "스냅샷 없음", ok: false },
  unavailable: { label: "스키마 관측 조회 불가", ok: false },
  unsupported_engine: { label: "스키마 관측 미지원 엔진", ok: false },
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

/** "last confirmed at T, not seen since", which is the strongest thing this
 * surface can say about a schema that stopped appearing in the catalog read.
 * A DROP SCHEMA and a read that could not reach the schema leave IDENTICAL
 * evidence, so it is never drawn as a drop. Returns null when the cluster was
 * fully confirmed, so the ordinary case gains no chrome. */
function NotSeen({ d }: { d: SchemaChangesResponse }) {
  const names = d.ddl_detection?.unconfirmed_schemas ?? [];
  if (names.length === 0) return null;
  const last = d.observation?.last_confirmed;
  return (
    <div className="text-[11px] text-amber-300 mt-1.5 leading-relaxed">
      {names.join(", ")} 스키마는 최근 카탈로그 읽기에서 확인되지 않았습니다
      {last ? ` (마지막 확인 ${last})` : ""}. 삭제됐을 수도 있고 읽기가 도달하지
      못한 것일 수도 있어 삭제로 단정하지 않습니다.
    </div>
  );
}

// The verdict an EMPTY list carries. One branch per top-level `status`, written
// as an if-chain rather than the lookup table this used to be: a table key is
// indistinguishable from a comment or a type to a source-parsing guard, so the
// whole operator-facing half of this panel could be deleted with the suite
// green. tests/unit/api/test_schema_changes_panel_states.py parses these guards,
// asserts which branch each status REACHES, and holds the state matrix that
// api/dashboard/handler.py documents beside its `status` derivation.
// Order is not load-bearing: the guards are mutually exclusive equality tests on
// one field.
function EmptyVerdict({ d }: { d: SchemaChangesResponse }) {
  const measured = (
    <div className="text-[11px] text-zinc-500 mt-1 tabular-nums">
      DDL 비교 {d.ddl_detection?.schemas_compared ?? 0}개 schema · 행 수 비교{" "}
      {d.row_deltas?.tables_compared ?? 0}개 table
    </div>
  );
  // The compensating channel for the deleted absence inference, rendered in EVERY
  // branch below and not only the negative one: a schema the collector can no
  // longer confirm is news whatever the headline says, and putting it in one branch
  // is how it ended up in no branch at all.
  const notSeen = <NotSeen d={d} />;
  if (d.status === "no_changes") {
    // The ONLY branch that may read as an absence of change: every source
    // compared, across every schema it holds.
    return (
      <div className="p-6 text-sm">
        <div className="text-zinc-400">이 구간에서 감지된 변경 없음</div>
        {measured}
        {notSeen}
      </div>
    );
  }
  if (d.status === "partial") {
    return (
      <div className="p-6 text-sm">
        <div className="text-amber-300">
          일부 신호만 판정됨: 변경 없음이라고 볼 수 없음
        </div>
        {measured}
        {notSeen}
      </div>
    );
  }
  if (d.status === "not_collected") {
    return (
      <div className="p-6 text-sm">
        <div className="text-amber-300">
          수집 이력이 없어 변경 여부를 판정할 수 없음
        </div>
        {notSeen}
      </div>
    );
  }
  if (d.status === "insufficient_history") {
    return (
      <div className="p-6 text-sm">
        <div className="text-amber-300">
          비교 가능한 이력이 부족해 변경 여부를 판정할 수 없음
        </div>
        {notSeen}
      </div>
    );
  }
  // Deploy skew. This is a static export, so it can meet a payload from an api
  // Lambda newer than itself. An unrecognised status is UNKNOWN, never a clean
  // bill of health: that is the difference from the anomalies panel, where the
  // old API predated the field entirely and degrading to the neutral copy was
  // the deliberate choice.
  return (
    <div className="p-6 text-sm">
      <div className="text-amber-300">
        변경 여부를 판정할 수 없음 (알 수 없는 응답 상태)
      </div>
      {measured}
      {notSeen}
    </div>
  );
}

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
          {!KNOWN_CHANGE.includes(c.change_type) && (
            <span
              className="text-amber-300"
              title="이 변경 유형을 해석할 수 있는 화면 버전이 아닙니다. 위 유형 이름과 테이블 이름은 서버가 보낸 값입니다."
            >
              {UNKNOWN_TYPE}
            </span>
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
  // The frontend is a static export deployed separately from the API Lambda, so
  // it can meet a payload predating these fields. Unknown, never "ok".
  const coll = data?.collection;
  const obs = data?.observation;
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
            <EmptyVerdict d={data} />
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

          {shown > 0 &&
            (data.ddl_detection?.unconfirmed_schemas ?? []).length > 0 && (
              <div className="px-4 py-2.5 border-t border-zinc-800">
                <NotSeen d={data} />
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
              {...(OBSERVATION_CHIP[obs?.status ?? "unavailable"] ??
                OBSERVATION_CHIP.unavailable)}
            />
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
