"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  fetchOnboardingTemplate,
  testClusterConnection,
  type OnboardingTemplate,
  type TestConnectionResult,
} from "@/lib/api-client";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";

// ── Helpers ──────────────────────────────────────────────────────────────────

function copyToClipboard(text: string) {
  navigator.clipboard.writeText(text).catch(() => {
    // Fallback for older browsers
    const el = document.createElement("textarea");
    el.value = text;
    document.body.appendChild(el);
    el.select();
    document.execCommand("copy");
    document.body.removeChild(el);
  });
}

function downloadJson(content: string, filename: string) {
  const blob = new Blob([content], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ── Step 1: Spoke role template ───────────────────────────────────────────────

function StepNumber({ n }: { n: number }) {
  return (
    <span className="inline-flex items-center justify-center w-6 h-6 rounded-full border border-emerald-300/40 bg-emerald-300/10 text-emerald-300 text-[11px] font-semibold flex-shrink-0">
      {n}
    </span>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-start gap-3 text-sm">
      <span className="text-zinc-500 font-medium w-32 flex-shrink-0">
        {label}
      </span>
      <span className="font-mono text-zinc-200 break-all">{value}</span>
    </div>
  );
}

interface TemplateStepProps {
  tpl: OnboardingTemplate;
  remediation: boolean;
  onToggleRemediation: () => void;
  loadingRemediation: boolean;
  copied: boolean;
  onCopy: () => void;
}

function TemplateStep({
  tpl,
  remediation,
  onToggleRemediation,
  loadingRemediation,
  copied,
  onCopy,
}: TemplateStepProps) {
  const deployCmd = `aws cloudformation deploy \\
  --template-file dbops-spoke-role.json \\
  --stack-name dbops-spoke-role \\
  --capabilities CAPABILITY_NAMED_IAM`;

  return (
    <div className="space-y-5">
      {/* Hub account info */}
      <div className="border border-zinc-800 bg-zinc-900/30 px-5 py-4 space-y-3">
        <div className="text-xs text-zinc-500 font-medium uppercase tracking-wider mb-3">
          Hub 계정 정보
        </div>
        <InfoRow label="Hub Account ID" value={tpl.hub_account_id} />
        <InfoRow label="Hub Role ARN" value={tpl.hub_role_arn} />
        <InfoRow label="Spoke Role Name" value={tpl.role_name} />
      </div>

      {/* Remediation toggle */}
      <div className="flex items-center justify-between border border-zinc-800 bg-zinc-900/30 px-5 py-3">
        <div>
          <div className="text-sm text-zinc-200 font-medium">
            원격 조치(Remediation) 포함
          </div>
          <div className="text-xs text-zinc-500 mt-0.5">
            에이전트가 파라미터 수정·재시작 등 쓰기 작업을 수행할 수 있게
            합니다. 읽기 전용 모니터링만 필요하면 비활성으로 두세요.
          </div>
        </div>
        <button
          onClick={onToggleRemediation}
          disabled={loadingRemediation}
          aria-pressed={remediation}
          aria-label="원격 조치 포함 토글"
          className={`relative inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full transition-colors focus:outline-none disabled:opacity-40 ${
            remediation ? "bg-emerald-400" : "bg-zinc-700"
          }`}
        >
          <span
            className={`inline-block h-3.5 w-3.5 rounded-full bg-white shadow-sm transition-transform ${
              remediation ? "translate-x-4" : "translate-x-1"
            }`}
          />
        </button>
      </div>

      {/* Template JSON */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs text-zinc-500 font-medium uppercase tracking-wider">
            CloudFormation 템플릿
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={onCopy}
              className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
            >
              {copied ? "복사됨 ✓" : "복사"}
            </button>
            <button
              onClick={() =>
                downloadJson(tpl.template, "dbops-spoke-role.json")
              }
              className="text-xs px-3 py-1.5 border border-zinc-700 text-zinc-400 hover:border-zinc-500 hover:text-zinc-100 transition-colors"
            >
              다운로드
            </button>
          </div>
        </div>
        <pre className="bg-zinc-950 border border-zinc-800 text-zinc-300 text-xs font-mono p-4 overflow-x-auto max-h-64 overflow-y-auto leading-relaxed whitespace-pre">
          {tpl.template}
        </pre>
      </div>

      {/* Deploy instructions */}
      <div className="border border-zinc-800 bg-zinc-900/30 px-5 py-4 space-y-3 text-sm text-zinc-400 leading-relaxed">
        <p>
          <strong className="text-zinc-200">배포 방법</strong> — 멤버 계정의 AWS
          CLI에서 아래 명령을 실행하세요. 파일을 다운로드한 디렉터리에서
          실행해야 합니다.
        </p>
        <pre className="bg-zinc-950 border border-zinc-800 text-emerald-300/80 text-xs font-mono px-4 py-3 overflow-x-auto">
          {deployCmd}
        </pre>
        <p className="text-xs text-zinc-500">
          배포가 완료되면 아래 Step 2에서 연결을 확인한 뒤, Step 3에서
          클러스터를 등록하세요.
        </p>
      </div>
    </div>
  );
}

// ── Step 2: Connection test ───────────────────────────────────────────────────

function StepStatusBadge({
  status,
}: {
  status: "ok" | "failed" | "skipped" | "warning";
}) {
  const map: Record<string, string> = {
    ok: "text-emerald-300 border-emerald-400/40 bg-emerald-400/10",
    failed: "text-rose-300 border-rose-400/40 bg-rose-400/10",
    skipped: "text-zinc-400 border-zinc-600 bg-zinc-800/50",
    warning: "text-amber-300 border-amber-400/40 bg-amber-400/10",
  };
  const label: Record<string, string> = {
    ok: "OK",
    failed: "FAIL",
    skipped: "SKIP",
    warning: "WARN",
  };
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 border text-[10px] font-mono font-semibold uppercase tracking-wider ${
        map[status] ?? map.skipped
      }`}
    >
      {label[status] ?? status}
    </span>
  );
}

interface ConnectionStepProps {
  accountId: string;
  setAccountId: (v: string) => void;
  clusterId: string;
  setClusterId: (v: string) => void;
  region: string;
  setRegion: (v: string) => void;
  testing: boolean;
  onTest: () => void;
  result: TestConnectionResult | null;
  testError: string | null;
}

function ConnectionStep({
  accountId,
  setAccountId,
  clusterId,
  setClusterId,
  region,
  setRegion,
  testing,
  onTest,
  result,
  testError,
}: ConnectionStepProps) {
  const inputCls =
    "w-full bg-zinc-950 border border-zinc-700 text-zinc-100 text-sm px-3 py-2 focus:outline-none focus:border-emerald-500/60 disabled:opacity-40 font-mono placeholder:text-zinc-600";

  return (
    <div className="space-y-5">
      <div className="border border-zinc-800 bg-zinc-900/30 px-5 py-4 space-y-4">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="block text-xs text-zinc-400 mb-1.5 font-medium">
              Account ID
            </label>
            <input
              type="text"
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              placeholder="123456789012"
              disabled={testing}
              className={inputCls}
              aria-label="AWS Account ID"
            />
            <p className="mt-1 text-[11px] text-zinc-600">
              멤버 계정 12자리 숫자
            </p>
          </div>
          <div>
            <label className="block text-xs text-zinc-400 mb-1.5 font-medium">
              Cluster ID
            </label>
            <input
              type="text"
              value={clusterId}
              onChange={(e) => setClusterId(e.target.value)}
              placeholder="my-aurora-cluster"
              disabled={testing}
              className={inputCls}
              aria-label="Aurora cluster identifier"
            />
            <p className="mt-1 text-[11px] text-zinc-600">
              Aurora 클러스터 식별자
            </p>
          </div>
          <div>
            <label className="block text-xs text-zinc-400 mb-1.5 font-medium">
              Region
            </label>
            <input
              type="text"
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              placeholder="ap-northeast-2"
              disabled={testing}
              className={inputCls}
              aria-label="AWS region"
            />
            <p className="mt-1 text-[11px] text-zinc-600">
              클러스터가 위치한 리전
            </p>
          </div>
        </div>

        {accountId && (
          <div className="text-xs text-zinc-500 font-mono">
            <span className="text-zinc-600">Spoke role ARN: </span>
            <span className="text-zinc-400">
              arn:aws:iam::{accountId}:role/dbops-spoke-role
            </span>
          </div>
        )}

        <div>
          <button
            onClick={onTest}
            disabled={testing || !accountId || !clusterId || !region}
            className="text-xs font-medium px-5 py-2.5 bg-emerald-400/90 text-zinc-950 hover:bg-emerald-300 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {testing ? "테스트 중…" : "테스트"}
          </button>
        </div>
      </div>

      {/* Test error */}
      {testError && (
        <div className="px-4 py-3 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs font-mono">
          {testError}
        </div>
      )}

      {/* Test result */}
      {result && (
        <div
          className={`border px-5 py-4 space-y-3 ${
            result.ok
              ? "border-emerald-500/30 bg-emerald-500/5"
              : "border-rose-500/30 bg-rose-500/5"
          }`}
        >
          <div
            className={`text-sm font-medium ${
              result.ok ? "text-emerald-300" : "text-rose-300"
            }`}
          >
            {result.ok ? "연결 성공" : "연결 실패 — 아래 단계를 확인하세요"}
          </div>
          <div className="space-y-2">
            {result.steps.map((step, i) => (
              <div key={i} className="flex items-start gap-3 text-xs font-mono">
                <StepStatusBadge status={step.status} />
                <div className="flex-1 min-w-0">
                  <span className="text-zinc-300">{step.name}</span>
                  {step.engine && (
                    <span className="ml-2 text-zinc-500">
                      {step.engine} {step.version}
                    </span>
                  )}
                  {step.endpoint && (
                    <div className="text-zinc-500 mt-0.5 break-all">
                      {step.endpoint}
                    </div>
                  )}
                  {step.error && (
                    <div className="text-rose-300/80 mt-0.5 break-words">
                      {step.error}
                    </div>
                  )}
                  {step.note && (
                    <div className="text-zinc-500 mt-0.5 break-words leading-relaxed">
                      {step.note}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Step 3: Register CTA ─────────────────────────────────────────────────────

function RegisterStep() {
  return (
    <div className="space-y-4">
      <div className="border border-zinc-800 bg-zinc-900/30 px-5 py-4 text-sm text-zinc-400 leading-relaxed space-y-2">
        <p>
          스포크 역할 배포와 연결 확인이 완료되면{" "}
          <strong className="text-zinc-200">Clusters</strong> 페이지에서
          클러스터를 탐색하고 등록합니다.
        </p>
        <p className="text-xs text-zinc-500">
          Clusters 페이지의 <code className="text-zinc-400">Discover</code>{" "}
          기능이 멤버 계정의 Aurora 클러스터를 자동으로 탐색합니다. 탐색된
          클러스터를 선택해 한번에 등록할 수 있습니다.
        </p>
      </div>
      <Link
        href="/clusters"
        className="inline-flex items-center gap-2 text-xs font-medium px-5 py-2.5 bg-emerald-400/90 text-zinc-950 hover:bg-emerald-300 transition-colors"
      >
        Clusters 페이지로 이동
        <span aria-hidden="true">→</span>
      </Link>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function OnboardingPage() {
  const [loading, setLoading] = useState(true);
  const [adminOnly, setAdminOnly] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Template state
  const [tpl, setTpl] = useState<OnboardingTemplate | null>(null);
  const [remediation, setRemediation] = useState(false);
  const [loadingRemediation, setLoadingRemediation] = useState(false);
  const [copied, setCopied] = useState(false);

  // Connection test state
  const [accountId, setAccountId] = useState("");
  const [clusterId, setClusterId] = useState("");
  const [region, setRegion] = useState("");
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<TestConnectionResult | null>(
    null,
  );
  const [testError, setTestError] = useState<string | null>(null);

  const load = useCallback((withRemediation: boolean) => {
    setLoadingRemediation(true);
    fetchOnboardingTemplate({ remediation: withRemediation || undefined })
      .then((d) => {
        setTpl(d);
        setLoadError(null);
        setAdminOnly(false);
      })
      .catch((e: unknown) => {
        const msg = e instanceof Error ? e.message : String(e);
        if (msg === "admin only") {
          setAdminOnly(true);
        } else {
          setLoadError(msg);
        }
      })
      .finally(() => {
        setLoading(false);
        setLoadingRemediation(false);
      });
  }, []);

  useEffect(() => {
    load(false);
  }, [load]);

  const handleToggleRemediation = () => {
    const next = !remediation;
    setRemediation(next);
    load(next);
  };

  const handleCopy = () => {
    if (!tpl) return;
    copyToClipboard(tpl.template);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleTest = async () => {
    if (!accountId || !clusterId || !region) return;
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    try {
      const result = await testClusterConnection({
        cluster_id: clusterId,
        region,
        spoke_role_arn: `arn:aws:iam::${accountId}:role/dbops-spoke-role`,
      });
      setTestResult(result);
    } catch (e: unknown) {
      setTestError(e instanceof Error ? e.message : String(e));
    } finally {
      setTesting(false);
    }
  };

  // ── Admin-only notice ───────────────────────────────────────────────────────

  if (!loading && adminOnly) {
    return (
      <PageBody>
        <PageHeader
          eyebrow="Configure"
          title="Onboarding"
          description="멤버 계정 연결 위저드 (관리자 전용)"
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
        title="Onboarding"
        description="멤버 AWS 계정에 스포크 역할을 배포하고 DBOps Hub에 연결합니다."
      />

      {loadError && (
        <div className="mb-6 px-3 py-2 border border-rose-500/40 bg-rose-500/10 text-rose-300 text-xs font-mono">
          {loadError}
        </div>
      )}

      {loading ? (
        <div className="text-sm text-zinc-500">불러오는 중…</div>
      ) : (
        <>
          {/* ── Step 1: 스포크 역할 생성 ── */}
          <Section
            eyebrow="Step 1"
            title="스포크 역할 생성"
            description="멤버 계정에 DBOps Hub가 AssumeRole할 수 있는 IAM 역할을 배포합니다."
          >
            <div className="flex items-center gap-3 mb-5">
              <StepNumber n={1} />
              <span className="text-sm text-zinc-400">
                아래 CloudFormation 템플릿을 멤버 계정에 배포하세요.
              </span>
            </div>
            {tpl ? (
              <TemplateStep
                tpl={tpl}
                remediation={remediation}
                onToggleRemediation={handleToggleRemediation}
                loadingRemediation={loadingRemediation}
                copied={copied}
                onCopy={handleCopy}
              />
            ) : (
              <div className="text-sm text-zinc-500">템플릿 로딩 실패</div>
            )}
          </Section>

          {/* ── Step 2: 연결 확인 ── */}
          <Section
            eyebrow="Step 2"
            title="연결 확인"
            description="스포크 역할 배포 후, Hub에서 AssumeRole + DescribeDBClusters가 정상 동작하는지 확인합니다."
          >
            <div className="flex items-center gap-3 mb-5">
              <StepNumber n={2} />
              <span className="text-sm text-zinc-400">
                멤버 계정 정보를 입력하고 테스트 버튼을 클릭하세요.
              </span>
            </div>
            <ConnectionStep
              accountId={accountId}
              setAccountId={setAccountId}
              clusterId={clusterId}
              setClusterId={setClusterId}
              region={region}
              setRegion={setRegion}
              testing={testing}
              onTest={handleTest}
              result={testResult}
              testError={testError}
            />
          </Section>

          {/* ── Step 3: 클러스터 등록 ── */}
          <Section
            eyebrow="Step 3"
            title="클러스터 등록"
            description="연결이 확인된 계정의 클러스터를 Clusters 페이지에서 탐색하고 등록합니다."
          >
            <div className="flex items-center gap-3 mb-5">
              <StepNumber n={3} />
              <span className="text-sm text-zinc-400">
                Clusters 페이지에서 멤버 계정을 탐색해 클러스터를 등록합니다.
              </span>
            </div>
            <RegisterStep />
          </Section>
        </>
      )}
    </PageBody>
  );
}
