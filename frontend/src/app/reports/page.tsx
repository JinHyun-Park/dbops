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
        eyebrow="자동화"
        title="리포트"
        description="report_generator Lambda가 cron으로 작성한 성능 요약 보고서가 여기에 누적됩니다."
      />
      {reports.length === 0 ? (
        <EmptyState
          eyebrow="리포트 없음"
          title="아직 생성된 리포트가 없습니다"
          description="ETL이 메트릭을 충분히 모으면 report_generator가 일/주간 요약을 생성합니다."
          secondary={{ href: "/chat", label: "채팅으로 즉시 생성하기" }}
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
