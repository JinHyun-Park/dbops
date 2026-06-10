"use client";

import { useEffect, useMemo, useState } from "react";
import {
  fetchSchemaGraph,
  type SchemaGraphResponse,
  type SchemaGraphTable,
} from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";
import { fmtBytes, fmtExact } from "@/lib/format";
import { isMysql } from "@/lib/engine";
import { useSelectedCluster } from "@/lib/use-selected-cluster";
import { ClusterPicker } from "@/components/design-system/cluster-picker";

export default function SchemaPage() {
  const { clusters, selected: selectedCluster } = useSelectedCluster();
  // FK lineage reads pg_constraint — PG only. Guard MySQL selections up front
  // instead of letting the run fail server-side with a cryptic error.
  const mysqlSelected = isMysql(
    clusters.find((c) => c.cluster_id === selectedCluster)?.engine,
  );
  const [schema, setSchema] = useState<string>("public");
  const [data, setData] = useState<SchemaGraphResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selectedTable, setSelectedTable] = useState<string | null>(null);

  const load = async () => {
    if (!selectedCluster) return;
    setLoading(true);
    setErr(null);
    setSelectedTable(null);
    try {
      const r = await fetchSchemaGraph(selectedCluster, schema || "public");
      setData(r);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setData(null);
    setErr(null);
    setSelectedTable(null);
  }, [selectedCluster]);

  return (
    <PageBody>
      <PageHeader
        eyebrow="모니터"
        title="Schema Lineage"
        description="현재 클러스터의 외래키(FK) 관계를 라이브로 추출해 표 의존성을 시각화합니다. PostgreSQL 전용."
        actions={
          <div className="flex items-center gap-2">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">
              Cluster
            </label>
            <ClusterPicker selected={selectedCluster} />
          </div>
        }
      />

      {!selectedCluster ? (
        <EmptyState
          title="클러스터가 없습니다"
          description="Clusters 페이지에서 먼저 PostgreSQL 클러스터를 등록하세요."
        />
      ) : (
        <>
          <div className="bg-zinc-900/50 border border-zinc-800 px-4 py-3 flex flex-wrap items-center gap-3 mb-6">
            <label className="text-[10px] uppercase tracking-wider text-zinc-500">
              Schema
            </label>
            <input
              value={schema}
              onChange={(e) => setSchema(e.target.value)}
              placeholder="public"
              className="bg-zinc-950 border border-zinc-700 text-zinc-200 text-xs px-2 py-1 font-mono w-40"
            />
            <button
              type="button"
              onClick={load}
              disabled={loading || !selectedCluster || mysqlSelected}
              title={
                mysqlSelected ? "FK 그래프는 PostgreSQL 전용입니다" : undefined
              }
              className="text-xs font-medium px-3 py-1 bg-amber-500 text-zinc-950 hover:bg-amber-400 disabled:opacity-50 transition-colors ml-auto"
            >
              {loading ? "추출 중…" : data ? "새로고침" : "FK 추출 실행"}
            </button>
          </div>

          {mysqlSelected && (
            <div className="mb-4 text-xs text-amber-300 border border-amber-500/40 bg-amber-500/10 px-3 py-2">
              선택된 클러스터는 MySQL입니다 — FK 그래프는 pg_constraint 기반의
              PostgreSQL 전용 기능입니다. 우측 상단에서 PostgreSQL 클러스터로
              전환하세요.
            </div>
          )}

          {err && (
            <div className="mb-4 text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              {err}
            </div>
          )}
          {data?.error && (
            <div className="mb-4 text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
              {data.error}
              {data.message && (
                <div className="mt-1 text-[11px] text-zinc-400 font-mono">
                  {data.message}
                </div>
              )}
            </div>
          )}
          {data?.info && !data?.error && (
            <div className="mb-4 text-xs text-zinc-300 border border-zinc-700 bg-zinc-900/40 px-3 py-2">
              {data.info}
            </div>
          )}

          {data && !data.error && (
            <>
              <SummaryTiles data={data} />
              <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-4 mt-6">
                <GraphCanvas
                  data={data}
                  selected={selectedTable}
                  onSelect={setSelectedTable}
                />
                <TableSidebar
                  data={data}
                  selected={selectedTable}
                  onSelect={setSelectedTable}
                />
              </div>
            </>
          )}

          {!data && !loading && !err && (
            <div className="text-zinc-500 text-sm">
              <span className="text-amber-300">FK 추출 실행</span> 버튼을 누르면
              pg_constraint를 조회해 외래키 그래프를 만듭니다.
            </div>
          )}
        </>
      )}
    </PageBody>
  );
}

// ---------------------------------------------------------------------------
// Summary tiles
// ---------------------------------------------------------------------------

function SummaryTiles({ data }: { data: SchemaGraphResponse }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      <Tile
        label="Tables"
        value={fmtExact(data.tables_count ?? data.tables.length)}
      />
      <Tile
        label="Foreign keys"
        value={fmtExact(data.edges_count ?? data.edges.length)}
      />
      <Tile
        label="Isolated"
        value={fmtExact(data.isolated_count ?? 0)}
        sub="들어오고 나가는 FK 없음"
        tone={(data.isolated_count ?? 0) > 0 ? "amber" : "zinc"}
      />
      <Tile
        label="Hub tables"
        value={fmtExact(
          data.tables.filter((t) => t.fk_in + t.fk_out >= 3).length,
        )}
        sub="FK 합계 ≥ 3"
      />
    </div>
  );
}

type Tone = "amber" | "zinc";
function Tile({
  label,
  value,
  sub,
  tone = "zinc",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: Tone;
}) {
  return (
    <div
      className={`border px-3 py-2 ${
        tone === "amber"
          ? "border-amber-500/40 bg-amber-500/5"
          : "border-zinc-800 bg-zinc-900/50"
      }`}
    >
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div
        className={`text-xl font-mono mt-0.5 tabular-nums ${
          tone === "amber" ? "text-amber-300" : "text-zinc-100"
        }`}
      >
        {value}
      </div>
      {sub && <div className="text-[10px] text-zinc-600">{sub}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// SVG graph canvas — simple grid layout + bezier edges
// ---------------------------------------------------------------------------

const BOX_W = 160;
const BOX_H = 50;
const GAP_X = 30;
const GAP_Y = 28;
const PADDING = 24;

function GraphCanvas({
  data,
  selected,
  onSelect,
}: {
  data: SchemaGraphResponse;
  selected: string | null;
  onSelect: (name: string | null) => void;
}) {
  // Sort tables: hubs first (high degree), then alphabetical. Better than
  // pure alphabetical because frequently-referenced tables cluster near
  // the top-left where the eye lands first.
  const layout = useMemo(() => {
    const sorted = [...data.tables].sort((a, b) => {
      const da = a.fk_in + a.fk_out;
      const db = b.fk_in + b.fk_out;
      if (da !== db) return db - da;
      return a.table_name.localeCompare(b.table_name);
    });
    return sorted;
  }, [data.tables]);

  const cols = Math.max(2, Math.min(6, Math.ceil(Math.sqrt(layout.length))));
  const positions = useMemo(() => {
    const map = new Map<
      string,
      { x: number; y: number; cx: number; cy: number }
    >();
    layout.forEach((t, i) => {
      const col = i % cols;
      const row = Math.floor(i / cols);
      const x = PADDING + col * (BOX_W + GAP_X);
      const y = PADDING + row * (BOX_H + GAP_Y);
      map.set(t.table_name, { x, y, cx: x + BOX_W / 2, cy: y + BOX_H / 2 });
    });
    return map;
  }, [layout, cols]);

  const rows = Math.ceil(layout.length / cols);
  const svgWidth = PADDING * 2 + cols * BOX_W + (cols - 1) * GAP_X;
  const svgHeight =
    PADDING * 2 + Math.max(1, rows) * BOX_H + Math.max(0, rows - 1) * GAP_Y;

  // Edges involving the selected table get highlighted.
  const edgeHighlighted = (e: { source_table: string; target_table: string }) =>
    selected !== null &&
    (e.source_table === selected || e.target_table === selected);

  if (layout.length === 0) {
    return (
      <div className="bg-zinc-900/40 border border-zinc-800 p-6 text-zinc-500 text-sm">
        해당 스키마에 테이블이 없습니다.
      </div>
    );
  }

  return (
    <div className="bg-zinc-900/40 border border-zinc-800 overflow-auto">
      <div className="px-3 py-2 border-b border-zinc-800 text-[10px] uppercase tracking-wider text-zinc-500">
        graph · 테이블을 클릭하면 해당 FK가 하이라이트됩니다
      </div>
      <svg
        width={svgWidth}
        height={svgHeight}
        viewBox={`0 0 ${svgWidth} ${svgHeight}`}
        className="block min-w-full"
        style={{ background: "transparent" }}
      >
        <defs>
          <marker
            id="arrow-zinc"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#52525b" />
          </marker>
          <marker
            id="arrow-amber"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="7"
            markerHeight="7"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="#fbbf24" />
          </marker>
        </defs>

        {data.edges.map((e, i) => {
          const a = positions.get(e.source_table);
          const b = positions.get(e.target_table);
          if (!a || !b) return null;
          const highlighted = edgeHighlighted(e);
          // Cubic bezier — control points are halfway between source and
          // target on the X axis, pulled toward the source/target Y to
          // create a gentle curve rather than a sharp diagonal.
          const dx = b.cx - a.cx;
          const c1x = a.cx + dx * 0.5;
          const c1y = a.cy;
          const c2x = b.cx - dx * 0.5;
          const c2y = b.cy;
          return (
            <path
              key={i}
              d={`M ${a.cx} ${a.cy} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${b.cx} ${b.cy}`}
              fill="none"
              stroke={highlighted ? "#fbbf24" : "#3f3f46"}
              strokeWidth={highlighted ? 1.6 : 0.9}
              opacity={highlighted || !selected ? 0.85 : 0.25}
              markerEnd={highlighted ? "url(#arrow-amber)" : "url(#arrow-zinc)"}
            />
          );
        })}

        {layout.map((t) => {
          const p = positions.get(t.table_name)!;
          const isSelected = selected === t.table_name;
          const isHub = t.fk_in + t.fk_out >= 3;
          const isIsolated = t.isolated;
          const stroke = isSelected
            ? "#fbbf24"
            : isHub
              ? "#0ea5e9"
              : isIsolated
                ? "#71717a"
                : "#52525b";
          const fill = isSelected
            ? "#fbbf24"
            : isHub
              ? "rgba(14,165,233,0.08)"
              : "rgba(24,24,27,0.95)";
          const textFill = isSelected ? "#18181b" : "#e4e4e7";
          return (
            <g
              key={t.table_name}
              onClick={() =>
                onSelect(selected === t.table_name ? null : t.table_name)
              }
              style={{ cursor: "pointer" }}
            >
              <rect
                x={p.x}
                y={p.y}
                width={BOX_W}
                height={BOX_H}
                rx={2}
                fill={fill}
                stroke={stroke}
                strokeWidth={isSelected ? 1.5 : 1}
              />
              <text
                x={p.x + 8}
                y={p.y + 18}
                fill={textFill}
                fontSize="11"
                fontFamily="ui-monospace, monospace"
              >
                {t.table_name.length > 22
                  ? t.table_name.slice(0, 20) + "…"
                  : t.table_name}
              </text>
              <text
                x={p.x + 8}
                y={p.y + 34}
                fill={isSelected ? "#3f3f46" : "#71717a"}
                fontSize="9"
                fontFamily="ui-monospace, monospace"
              >
                {fmtExact(t.row_count)} rows · {fmtBytes(t.size_bytes)}
              </text>
              <text
                x={p.x + BOX_W - 8}
                y={p.y + 18}
                textAnchor="end"
                fill={isSelected ? "#3f3f46" : "#a1a1aa"}
                fontSize="9"
                fontFamily="ui-monospace, monospace"
              >
                ←{t.fk_in} →{t.fk_out}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Side panel — selected table FK detail, or full table list
// ---------------------------------------------------------------------------

function TableSidebar({
  data,
  selected,
  onSelect,
}: {
  data: SchemaGraphResponse;
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  if (selected) {
    return (
      <SelectedTableDetail
        data={data}
        tableName={selected}
        onSelect={onSelect}
      />
    );
  }
  return <TableList data={data} onSelect={onSelect} />;
}

function SelectedTableDetail({
  data,
  tableName,
  onSelect,
}: {
  data: SchemaGraphResponse;
  tableName: string;
  onSelect: (name: string) => void;
}) {
  const table = data.tables.find((t) => t.table_name === tableName);
  const outgoing = data.edges.filter((e) => e.source_table === tableName);
  const incoming = data.edges.filter((e) => e.target_table === tableName);

  return (
    <div className="bg-zinc-900/40 border border-zinc-800 max-h-[680px] overflow-y-auto">
      <div className="px-3 py-2 border-b border-zinc-800 sticky top-0 bg-zinc-900/95 backdrop-blur">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">
          selected
        </div>
        <div className="text-sm font-mono text-amber-300 break-all">
          {tableName}
        </div>
        {table && (
          <div className="text-[10px] text-zinc-500 mt-0.5 font-mono">
            {fmtExact(table.row_count)} rows · {fmtBytes(table.size_bytes)}
          </div>
        )}
      </div>

      <FkSection
        label={`→ outgoing (${outgoing.length})`}
        hint="이 테이블이 다른 테이블을 참조"
        edges={outgoing}
        peerKey="target_table"
        onSelect={onSelect}
      />
      <FkSection
        label={`← incoming (${incoming.length})`}
        hint="다른 테이블이 이 테이블을 참조 — 변경시 영향도 확인 필요"
        edges={incoming}
        peerKey="source_table"
        onSelect={onSelect}
      />
    </div>
  );
}

function FkSection({
  label,
  hint,
  edges,
  peerKey,
  onSelect,
}: {
  label: string;
  hint: string;
  edges: SchemaGraphResponse["edges"];
  peerKey: "source_table" | "target_table";
  onSelect: (name: string) => void;
}) {
  return (
    <div className="border-b border-zinc-800">
      <div className="px-3 py-2 text-[10px] uppercase tracking-wider text-zinc-500">
        {label}
      </div>
      <div className="text-[10px] text-zinc-600 px-3 -mt-1 mb-1">{hint}</div>
      {edges.length === 0 ? (
        <div className="px-3 pb-3 text-[11px] text-zinc-600">없음</div>
      ) : (
        <ul className="divide-y divide-zinc-800/60">
          {edges.map((e) => (
            <li key={e.constraint_name} className="px-3 py-1.5">
              <button
                type="button"
                onClick={() => onSelect(e[peerKey])}
                className="text-xs font-mono text-amber-300 hover:text-amber-200 underline underline-offset-2 break-all"
              >
                {e[peerKey]}
              </button>
              <div className="text-[10px] text-zinc-500 font-mono mt-0.5">
                {e.source_columns} → {e.target_columns}
              </div>
              <div
                className="text-[10px] text-zinc-600 font-mono truncate"
                title={e.definition}
              >
                {e.constraint_name}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TableList({
  data,
  onSelect,
}: {
  data: SchemaGraphResponse;
  onSelect: (name: string) => void;
}) {
  const sorted = useMemo(
    () =>
      [...data.tables].sort((a, b) => {
        const da = a.fk_in + a.fk_out;
        const db = b.fk_in + b.fk_out;
        if (da !== db) return db - da;
        return a.table_name.localeCompare(b.table_name);
      }),
    [data.tables],
  );

  return (
    <div className="bg-zinc-900/40 border border-zinc-800 max-h-[680px] overflow-y-auto">
      <div className="px-3 py-2 border-b border-zinc-800 sticky top-0 bg-zinc-900/95 backdrop-blur text-[10px] uppercase tracking-wider text-zinc-500">
        tables · 클릭하면 FK 상세
      </div>
      <ul className="divide-y divide-zinc-800/60">
        {sorted.map((t) => (
          <li key={t.table_name}>
            <button
              type="button"
              onClick={() => onSelect(t.table_name)}
              className="w-full text-left px-3 py-1.5 hover:bg-zinc-800/40 transition-colors flex items-baseline gap-2"
            >
              <span
                className={`text-xs font-mono break-all flex-1 ${
                  t.isolated ? "text-zinc-500" : "text-zinc-200"
                }`}
              >
                {t.table_name}
              </span>
              <span className="text-[10px] text-zinc-500 font-mono tabular-nums whitespace-nowrap">
                ←{t.fk_in} →{t.fk_out}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
