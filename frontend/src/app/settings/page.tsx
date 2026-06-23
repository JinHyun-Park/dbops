"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchAppConfig,
  updateAppConfig,
  type AppConfigItem,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";

// ── Inline toggle — no design-system toggle exists yet ─────────────────────

function Toggle({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-6 w-11 flex-shrink-0 cursor-pointer items-center rounded-full border transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400 disabled:cursor-not-allowed disabled:opacity-40 ${
        checked
          ? "bg-emerald-400/90 border-emerald-400/70"
          : "bg-zinc-700 border-zinc-600"
      }`}
    >
      <span
        className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transition-transform ${
          checked ? "translate-x-6" : "translate-x-1"
        }`}
      />
    </button>
  );
}

// ── Provenance line ─────────────────────────────────────────────────────────

function Provenance({ item }: { item: AppConfigItem }) {
  if (!item.updated_by && !item.updated_at) return null;
  const ts = item.updated_at
    ? new Date(item.updated_at).toLocaleString("ko-KR", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;
  return (
    <div className="mt-1.5 text-[11px] font-mono text-zinc-600">
      마지막 변경:{" "}
      {item.updated_by && (
        <span className="text-zinc-500">{item.updated_by}</span>
      )}
      {item.updated_by && ts && <span className="text-zinc-700"> · </span>}
      {ts && <span className="text-zinc-500">{ts}</span>}
    </div>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function SettingsPage() {
  const [items, setItems] = useState<AppConfigItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [adminOnly, setAdminOnly] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState<"success" | "error" | null>(
    null,
  );
  const [saveError, setSaveError] = useState<string | null>(null);

  // Draft values (controlled)
  const [reportEnabled, setReportEnabled] = useState(false);
  const [ticketingProvider, setTicketingProvider] = useState("none");

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setAdminOnly(false);
    fetchAppConfig()
      .then((d) => {
        setItems(d.items);
        const reportItem = d.items.find(
          (i) => i.key === "REPORT_DELIVERY_ENABLED",
        );
        const ticketItem = d.items.find((i) => i.key === "TICKETING_PROVIDER");
        setReportEnabled(reportItem?.value === "true");
        setTicketingProvider(ticketItem?.value ?? "none");
      })
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        if (msg === "admin only") {
          setAdminOnly(true);
        } else {
          setError(msg);
        }
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onSave = async () => {
    setSaving(true);
    setSaveResult(null);
    setSaveError(null);
    try {
      const data = await updateAppConfig({
        REPORT_DELIVERY_ENABLED: reportEnabled,
        TICKETING_PROVIDER: ticketingProvider.trim() || "none",
      });
      setItems(data.items);
      // Sync drafts from the authoritative server response
      const reportItem = data.items.find(
        (i) => i.key === "REPORT_DELIVERY_ENABLED",
      );
      const ticketItem = data.items.find((i) => i.key === "TICKETING_PROVIDER");
      setReportEnabled(reportItem?.value === "true");
      setTicketingProvider(ticketItem?.value ?? "none");
      setSaveResult("success");
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setSaveError(msg);
      setSaveResult("error");
    } finally {
      setSaving(false);
    }
  };

  const reportItem = items.find((i) => i.key === "REPORT_DELIVERY_ENABLED");
  const ticketItem = items.find((i) => i.key === "TICKETING_PROVIDER");

  // ── Admin-only notice ───────────────────────────────────────────────────

  if (!loading && adminOnly) {
    return (
      <PageBody>
        <PageHeader
          eyebrow="Configure"
          title="Settings"
          description="기능 토글 — 티켓팅 연동·리포트 전달 제어 (관리자 전용)"
        />
        <Section>
          <EmptyState
            eyebrow="접근 제한"
            title="관리자 전용 페이지"
            description="이 설정은 관리자만 변경할 수 있습니다."
          />
        </Section>
      </PageBody>
    );
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow="Configure"
        title="Settings"
        description="기능 토글 — 티켓팅 연동·리포트 전달 제어. 변경 사항은 즉시 적용됩니다."
      />

      {/* Load error */}
      {error && (
        <div className="mb-6 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-zinc-500">불러오는 중…</div>
      ) : (
        <>
          {/* ── Report delivery ── */}
          <Section
            eyebrow="Notifications"
            title="Report delivery"
            description="정기 운영 요약 리포트를 SNS · Slack 구독자에게 자동 발송합니다."
          >
            <div className="border border-zinc-800 bg-zinc-900/30">
              <div className="px-5 py-4 flex items-center justify-between gap-6">
                <div className="flex-1 min-w-0">
                  <div className="text-sm text-zinc-100 font-medium">
                    Report delivery
                  </div>
                  <div className="mt-0.5 text-xs text-zinc-500 leading-relaxed max-w-lg">
                    활성화하면 Tasks 페이지의 scheduled_report 결과가 SNS 토픽에
                    등록된 이메일·Slack 구독자에게 자동 발송됩니다. 구독자는
                    Alerts 페이지에서 추가하세요.
                  </div>
                  {reportItem && <Provenance item={reportItem} />}
                </div>
                <Toggle
                  checked={reportEnabled}
                  onChange={setReportEnabled}
                  disabled={saving}
                />
              </div>
            </div>
          </Section>

          {/* ── Ticketing provider ── */}
          <Section
            eyebrow="Integrations"
            title="Ticketing provider"
            description="이상 감지·RCA 결과를 외부 티켓 시스템에 자동 등록할 제공자를 지정합니다."
          >
            <div className="border border-zinc-800 bg-zinc-900/30">
              <div className="px-5 py-4">
                <div className="text-sm text-zinc-100 font-medium mb-0.5">
                  Provider name
                </div>
                <div className="text-xs text-zinc-500 leading-relaxed max-w-lg mb-3">
                  현재 지원하는 값: <code className="text-zinc-400">none</code>{" "}
                  (비활성). <code className="text-zinc-400">jira</code> 등 다른
                  값을 입력해도 코드에 연동 구현이 없으면 아무 동작도 하지
                  않습니다 — 제공자 연동을 먼저 구현한 뒤 값을 바꾸세요.
                </div>
                <input
                  type="text"
                  value={ticketingProvider}
                  onChange={(e) => setTicketingProvider(e.target.value)}
                  disabled={saving}
                  placeholder="none"
                  className="w-full max-w-xs bg-zinc-950 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-emerald-500/60 disabled:opacity-40 font-mono placeholder:text-zinc-600"
                  aria-label="Ticketing provider"
                />
                {ticketItem && <Provenance item={ticketItem} />}
              </div>
            </div>
          </Section>

          {/* ── Save bar ── */}
          <div className="flex items-center gap-4 pt-2">
            <button
              onClick={onSave}
              disabled={saving}
              className="text-xs font-medium px-5 py-2.5 bg-emerald-400/90 text-zinc-950 hover:bg-emerald-300 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {saving ? "저장 중…" : "저장"}
            </button>

            {saveResult === "success" && (
              <span className="text-xs text-emerald-400">저장되었습니다</span>
            )}
            {saveResult === "error" && saveError && (
              <span className="text-xs text-rose-400">{saveError}</span>
            )}
          </div>
        </>
      )}
    </PageBody>
  );
}
