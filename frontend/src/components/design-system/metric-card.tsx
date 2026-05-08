interface MetricCardProps {
  label: string;
  value: string | number;
  unit?: string;
  trend?: "up" | "down" | "stable";
}

export function MetricCard({ label, value, unit, trend }: MetricCardProps) {
  return (
    <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-4">
      <div className="text-xs text-zinc-400 uppercase tracking-wider mb-1">
        {label}
      </div>
      <div className="flex items-baseline gap-1">
        <span className="text-2xl font-semibold text-zinc-100">{value}</span>
        {unit && <span className="text-sm text-zinc-400">{unit}</span>}
        {trend && (
          <span
            className={`text-xs ml-2 ${
              trend === "up"
                ? "text-red-400"
                : trend === "down"
                  ? "text-emerald-400"
                  : "text-zinc-500"
            }`}
          >
            {trend === "up" ? "↑" : trend === "down" ? "↓" : "→"}
          </span>
        )}
      </div>
    </div>
  );
}
