"use client";

import { useState, useEffect } from "react";
import { ReportViewer } from "@/components/reports/report-viewer";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "https://vp8z6cdxcd.execute-api.ap-northeast-2.amazonaws.com";

export default function ReportsPage() {
  const [reports, setReports] = useState<any[]>([]);
  const [selectedReport, setSelectedReport] = useState<any>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/reports`)
      .then((r) => r.json())
      .then(setReports)
      .catch(console.error);
  }, []);

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 p-6">
      <h1 className="text-2xl font-bold mb-6">Reports</h1>
      <ReportViewer
        reports={reports}
        selectedReport={selectedReport}
        onSelect={setSelectedReport}
      />
    </div>
  );
}
