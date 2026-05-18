"use client";

import { useState } from "react";
import { createPortal } from "react-dom";

/**
 * Setup guide modal — shows DBAs exactly what to do BEFORE bulk Discover.
 *
 * Two tabs (PostgreSQL / MySQL). Each tab walks through:
 *  1. Create the dedicated `dbops_readonly` role
 *  2. Grant the minimum monitoring privileges
 *  3. Generate a random password
 *  4. Store the password in AWS Secrets Manager under the convention name
 *     `dbops/<cluster_id>/readonly` so Discover auto-attaches it
 *
 * The guide is discoverable via a small button in the page header but
 * lives behind a modal so it doesn't dominate the registration view for
 * users who already know the drill.
 */

interface Props {
  open: boolean;
  onClose: () => void;
  // Pre-fills the SQL snippets with the cluster_id the user is about to set up.
  // Optional — when missing, snippets use a placeholder.
  clusterId?: string;
  // Same for region (used in the CLI snippet for `aws secretsmanager create-secret`).
  region?: string;
}

type Engine = "postgres" | "mysql";

export function SetupGuideModal({ open, onClose, clusterId, region }: Props) {
  const [engine, setEngine] = useState<Engine>("postgres");
  const cid = clusterId || "<cluster_id>";
  const reg = region || "<region>";

  if (!open) return null;

  const sqlPg = `-- 1) DBOps 전용 read-only 롤 생성
CREATE ROLE dbops_readonly LOGIN PASSWORD '<생성한-비밀번호-붙여넣기>';

-- 2) DBOps에 필요한 최소 권한만 부여
GRANT pg_monitor TO dbops_readonly;            -- pg_stat_statements, pg_stat_activity 등
GRANT pg_read_all_settings TO dbops_readonly;
GRANT pg_read_all_stats TO dbops_readonly;     -- PG15+ — pg_stat_user_indexes / tables 포함
-- (선택) 특정 스키마 내부까지 인스펙션이 필요할 때만:
-- GRANT USAGE ON SCHEMA public TO dbops_readonly;
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO dbops_readonly;`;

  const sqlMysql = `-- 1) DBOps 전용 read-only 유저 생성
CREATE USER 'dbops_readonly'@'%' IDENTIFIED BY '<생성한-비밀번호-붙여넣기>';

-- 2) DBOps에 필요한 최소 권한만 부여
GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'dbops_readonly'@'%';
GRANT SELECT ON performance_schema.* TO 'dbops_readonly'@'%';
GRANT SELECT ON information_schema.* TO 'dbops_readonly'@'%';
GRANT SELECT ON mysql.* TO 'dbops_readonly'@'%';  -- 유저 감사용
FLUSH PRIVILEGES;`;

  const passwordCli = `# 3) 강력한 랜덤 비밀번호 생성 (32자)
PASSWORD=$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)
echo "$PASSWORD"
# → 위 CREATE ROLE / CREATE USER 단계에서 이 값을 사용`;

  const secretsManagerCli = `# 4) DBOps 컨벤션 이름으로 AWS Secrets Manager에 등록
aws secretsmanager create-secret \\
  --region ${reg} \\
  --name "dbops/${cid}/readonly" \\
  --description "DBOps readonly access for ${cid}" \\
  --secret-string "{\\"username\\":\\"dbops_readonly\\",\\"password\\":\\"$PASSWORD\\"}"

# DBOps Discover가 이 시크릿을 자동으로 찾아 연결합니다 — ARN 수동 입력 불필요.`;

  const body = (
    <div
      className="fixed inset-0 z-50 bg-zinc-950/85 backdrop-blur flex items-center justify-center p-4 sm:p-6 md:p-10"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label="클러스터 설정 가이드"
    >
      <div
        className="w-full max-w-3xl max-h-[92vh] bg-zinc-900 border border-zinc-700 shadow-2xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between px-5 py-3 border-b border-zinc-800 flex-shrink-0">
          <div>
            <div className="font-mono text-[10px] tracking-[0.2em] uppercase text-amber-300">
              cluster setup guide
            </div>
            <div className="text-sm text-zinc-200 mt-0.5">
              DBOps 전용 계정 + Secrets Manager 등록 (프로덕션 권장)
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-zinc-100 text-xl leading-none px-2"
            aria-label="닫기"
          >
            ×
          </button>
        </header>

        <div className="px-5 py-3 border-b border-zinc-800 flex items-center gap-2">
          <button
            onClick={() => setEngine("postgres")}
            className={`text-xs px-3 py-1.5 border transition-colors ${
              engine === "postgres"
                ? "border-sky-500/50 text-sky-300 bg-sky-500/10"
                : "border-zinc-800 text-zinc-400 hover:text-zinc-200"
            }`}
          >
            PostgreSQL
          </button>
          <button
            onClick={() => setEngine("mysql")}
            className={`text-xs px-3 py-1.5 border transition-colors ${
              engine === "mysql"
                ? "border-orange-500/50 text-orange-300 bg-orange-500/10"
                : "border-zinc-800 text-zinc-400 hover:text-zinc-200"
            }`}
          >
            MySQL
          </button>
          {clusterId && (
            <span className="ml-auto text-[11px] text-zinc-500 font-mono">
              대상: {clusterId}
            </span>
          )}
        </div>

        <div className="flex-1 overflow-auto px-5 py-4 space-y-5">
          <section>
            <div className="text-xs text-zinc-400 mb-2">
              아래 SQL을{" "}
              <span className="font-mono text-zinc-200">
                대상 클러스터에서 admin(master)
              </span>{" "}
              계정으로 실행하세요. 생성되는 롤에는 모니터링/읽기 권한만 부여되며
              DDL, write, 롤 관리 권한은 포함되지 않습니다.
            </div>
            <Code text={engine === "postgres" ? sqlPg : sqlMysql} />
          </section>

          <section>
            <div className="text-xs text-zinc-400 mb-2">
              강력한 비밀번호 생성
            </div>
            <Code text={passwordCli} />
          </section>

          <section>
            <div className="text-xs text-zinc-400 mb-2">
              DBOps 네이밍 컨벤션으로{" "}
              <span className="font-mono text-zinc-200">
                AWS Secrets Manager
              </span>
              에 등록 — bulk Discover가 자동으로 찾아 연결합니다
            </div>
            <Code text={secretsManagerCli} />
          </section>

          <section className="border-t border-zinc-800 pt-4">
            <div className="text-[11px] text-zinc-500 leading-relaxed">
              <strong className="text-zinc-300">
                왜 전용 계정을 만들어야 하나요?
              </strong>
              <br />
              Aurora master 계정은 클러스터 전체에 대한 admin 권한을 가집니다.
              DBOps에 그 수준의 접근권을 부여하면 DBOps Lambda가 침해됐을 때
              모든 스키마와 자격증명이 노출됩니다. 위에서 만든{" "}
              <code>dbops_readonly</code> 롤은 모니터링 카탈로그와 테이블 통계는
              읽을 수 있지만 데이터 변경이나 롤 관리는 불가능 — blast radius가
              훨씬 작습니다.
              <br />
              <br />
              <strong className="text-zinc-300">컨벤션 이름:</strong>{" "}
              <code className="text-amber-300">
                dbops/&lt;cluster_id&gt;/readonly
              </code>
              <br />
              Bulk Discover가 이 시크릿을 발견하면{" "}
              <span className="text-emerald-400">convention</span> 배지와 함께
              자동 연결. 시크릿이 없으면 master 시크릿으로 폴백하면서{" "}
              <span className="text-amber-400">master_fallback</span> 경고가
              표시됩니다 — 런타임은 동작하지만 프로덕션 전환 전에 전용 계정을
              등록하는 게 좋습니다.
            </div>
          </section>
        </div>
      </div>
    </div>
  );

  if (typeof document === "undefined") return null;
  return createPortal(body, document.body);
}

function Code({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="relative group">
      <pre className="bg-zinc-950 border border-zinc-800 text-zinc-200 text-xs font-mono p-3 overflow-x-auto leading-relaxed whitespace-pre">
        {text}
      </pre>
      <button
        onClick={() => {
          if (typeof navigator !== "undefined" && navigator.clipboard) {
            navigator.clipboard.writeText(text).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            });
          }
        }}
        className="absolute top-2 right-2 text-[10px] px-2 py-1 bg-zinc-900 border border-zinc-700 text-zinc-400 hover:text-zinc-100 hover:border-amber-500/40 opacity-0 group-hover:opacity-100 transition-opacity"
      >
        {copied ? "복사됨!" : "복사"}
      </button>
    </div>
  );
}
