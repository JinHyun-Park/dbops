"use client";

interface Report {
  id: number;
  cluster_id: string;
  report_type: string;
  report_date: string;
  summary: string;
  created_at: string;
}

interface ReportViewerProps {
  reports: Report[];
  selectedReport: Report | null;
  onSelect: (report: Report) => void;
}

export function ReportViewer({ reports, selectedReport, onSelect }: ReportViewerProps) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="lg:col-span-1">
        <div className="bg-zinc-800 border border-zinc-700 rounded-lg overflow-hidden">
          <div className="px-4 py-3 border-b border-zinc-700 text-sm text-zinc-400">Reports</div>
          <div className="divide-y divide-zinc-700">
            {reports.length === 0 && (
              <div className="p-4 text-sm text-zinc-500">리포트가 없습니다</div>
            )}
            {reports.map((r) => (
              <button
                key={r.id}
                onClick={() => onSelect(r)}
                className={`w-full text-left px-4 py-3 hover:bg-zinc-750 transition-colors ${
                  selectedReport?.id === r.id ? "bg-zinc-700" : ""
                }`}
              >
                <div className="text-sm text-zinc-200">{r.report_date} — {r.report_type}</div>
                <div className="text-xs text-zinc-400 mt-1">{r.cluster_id}</div>
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="lg:col-span-2">
        <div className="bg-zinc-800 border border-zinc-700 rounded-lg p-6">
          {selectedReport ? (
            <>
              <h2 className="text-lg font-semibold text-zinc-100 mb-2">
                {selectedReport.report_date} — {selectedReport.report_type}
              </h2>
              <p className="text-sm text-zinc-400 mb-4">{selectedReport.cluster_id}</p>
              <div className="text-sm text-zinc-200 whitespace-pre-wrap">{selectedReport.summary}</div>
            </>
          ) : (
            <div className="text-center text-zinc-500 py-12">리포트를 선택해주세요</div>
          )}
        </div>
      </div>
    </div>
  );
}
