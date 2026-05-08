interface StatusBadgeProps {
  status: "healthy" | "warning" | "critical" | "unknown";
}

const styles = {
  healthy: "bg-emerald-900/50 text-emerald-300 border-emerald-700",
  warning: "bg-amber-900/50 text-amber-300 border-amber-700",
  critical: "bg-red-900/50 text-red-300 border-red-700",
  unknown: "bg-zinc-800 text-zinc-400 border-zinc-700",
};

export function StatusBadge({ status }: StatusBadgeProps) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${styles[status]}`}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${
          status === "healthy"
            ? "bg-emerald-400"
            : status === "warning"
              ? "bg-amber-400"
              : status === "critical"
                ? "bg-red-400"
                : "bg-zinc-500"
        }`}
      />
      {status}
    </span>
  );
}
