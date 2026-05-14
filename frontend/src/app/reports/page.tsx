"use client";

import { useState, useEffect } from "react";
import { ReportViewer } from "@/components/reports/report-viewer";
import { apiUrl } from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";

export default function ReportsPage() {
  const [reports, setReports] = useState<any[]>([]);
  const [selectedReport, setSelectedReport] = useState<any>(null);

  useEffect(() => {
    apiUrl("/api/reports")
      .then((url) => fetch(url))
      .then((r) => r.json())
      .then(setReports)
      .catch(console.error);
  }, []);

  return (
    <PageBody>
      <PageHeader
        eyebrow="automate"
        title="Reports"
        description="Scheduled performance summaries 자동 생성. report_generator Lambda가 cron으로 작성한 보고서가 여기에 누적됩니다."
      />
      {reports.length === 0 ? (
        <EmptyState
          eyebrow="no reports"
          title="No reports yet"
          description="ETL이 메트릭을 충분히 모으면 report_generator가 일/주간 요약을 생성합니다."
          secondary={{ href: "/chat", label: "Generate on demand via chat" }}
        />
      ) : (
        <ReportViewer
          reports={reports}
          selectedReport={selectedReport}
          onSelect={setSelectedReport}
        />
      )}
    </PageBody>
  );
}
