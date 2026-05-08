"use client";

interface AasChartProps {
  data: { ts: string; value: number }[];
}

export function AasChart({ data }: AasChartProps) {
  if (data.length === 0) {
    return (
      <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-8 text-center text-zinc-500">
        No metrics data available
      </div>
    );
  }

  const maxValue = Math.max(...data.map((d) => d.value), 1);

  return (
    <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-4">
      <div className="text-sm text-zinc-400 mb-3">
        Active Average Sessions (AAS)
      </div>
      <div className="flex items-end gap-0.5 h-32">
        {data.slice(-60).map((d, i) => (
          <div
            key={i}
            className="flex-1 bg-blue-500 rounded-t opacity-80 hover:opacity-100 transition-opacity"
            style={{ height: `${(d.value / maxValue) * 100}%` }}
            title={`${d.ts}: ${d.value.toFixed(2)}`}
          />
        ))}
      </div>
    </div>
  );
}
