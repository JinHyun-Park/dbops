"use client";

interface ToolStatusProps {
  name: string;
  status: "running" | "done";
}

export function ToolStatus({ name, status }: ToolStatusProps) {
  return (
    <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-zinc-800 text-zinc-300 text-sm font-mono">
      {status === "running" ? (
        <span className="inline-block w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
      ) : (
        <span className="inline-block w-2 h-2 rounded-full bg-emerald-400" />
      )}
      <span>{name}</span>
    </div>
  );
}
