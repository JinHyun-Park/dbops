"use client";

import { useEffect, useState } from "react";
import { ReportViewer } from "@/components/reports/report-viewer";
import { apiUrl, authedFetch } from "@/lib/api-client";
import {
  PageHeader,
  PageBody,
  EmptyState,
} from "@/components/design-system/page-shell";
import { useT } from "@/lib/i18n";

interface ReportRow {
  id: number;
  cluster_id: string;
  report_type: string;
  report_date: string;
  summary: string;
  created_at: string;
}

interface ReportDetail extends ReportRow {
  data?: string | object | null;
  s3_key?: string;
}

export default function ReportsPage() {
  const t = useT();
  const [reports, setReports] = useState<ReportRow[]>([]);
  const [selectedRow, setSelectedRow] = useState<ReportRow | null>(null);
  const [detail, setDetail] = useState<ReportDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  useEffect(() => {
    apiUrl("/api/reports")
      .then((url) => authedFetch(url))
      .then((r) => r.json())
      .then((rows) => {
        setReports(Array.isArray(rows) ? rows : []);
      })
      .catch(console.error);
  }, []);

  useEffect(() => {
    if (!selectedRow) {
      setDetail(null);
      return;
    }
    setDetailLoading(true);
    apiUrl(`/api/reports/${selectedRow.id}`)
      .then((url) => authedFetch(url))
      .then((r) => r.json())
      .then((row) => setDetail(row))
      .catch((e) => {
        console.error(e);
        setDetail(null);
      })
      .finally(() => setDetailLoading(false));
  }, [selectedRow]);

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("자동화")}
        title={t("리포트")}
        description={t(
          "report_generator Lambda가 매일 자정에 작성하는 클러스터별 운영 요약. AAS, 슬로우 쿼리, 알림, 스토리지 변화를 한 화면에 모아둡니다.",
        )}
      />
      {reports.length === 0 ? (
        <EmptyState
          eyebrow={t("리포트 없음")}
          title={t("아직 생성된 리포트가 없습니다")}
          description={t(
            "ETL이 메트릭을 충분히 모으면 report_generator 가 첫 일/주간 요약을 생성합니다. 즉시 받아보고 싶으면 채팅에서 요청해보세요.",
          )}
          secondary={{ href: "/chat", label: t("채팅으로 즉시 생성하기") }}
        />
      ) : (
        <ReportViewer
          reports={reports}
          selectedRow={selectedRow}
          detail={detail}
          detailLoading={detailLoading}
          onSelect={setSelectedRow}
        />
      )}
    </PageBody>
  );
}
