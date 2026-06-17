"use client";

import { useEffect, useMemo, useState } from "react";
import {
  PageBody,
  PageHeader,
  Section,
  EmptyState,
} from "@/components/design-system/page-shell";

interface Operation {
  tags?: string[];
  summary?: string;
  security?: unknown[];
  parameters?: { name: string; in: string }[];
}
type PathItem = Record<string, Operation>;
interface Spec {
  info?: { title?: string; version?: string; description?: string };
  paths: Record<string, PathItem>;
  tags?: { name: string }[];
}

const METHOD_STYLES: Record<string, string> = {
  get: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  post: "bg-amber-500/15 text-amber-300 border-amber-500/40",
  put: "bg-violet-500/15 text-violet-300 border-violet-500/40",
  delete: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  patch: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
};

export default function ApiDocsPage() {
  const [spec, setSpec] = useState<Spec | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    // openapi.json is shipped as a static asset (frontend/public/), regenerated
    // from the CDK route table by tools/openapi_gen.py (parity-tested in CI).
    fetch("/openapi.json", { cache: "no-store" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setSpec)
      .catch((e) => setErr(e instanceof Error ? e.message : "스펙 로드 실패"));
  }, []);

  const byTag = useMemo(() => {
    const out: Record<
      string,
      { path: string; method: string; op: Operation }[]
    > = {};
    if (spec) {
      for (const [path, item] of Object.entries(spec.paths)) {
        for (const [method, op] of Object.entries(item)) {
          const tag = op.tags?.[0] || "api";
          (out[tag] ||= []).push({ path, method, op });
        }
      }
      for (const list of Object.values(out)) {
        list.sort(
          (a, b) =>
            a.path.localeCompare(b.path) || a.method.localeCompare(b.method),
        );
      }
    }
    return out;
  }, [spec]);

  const tags = Object.keys(byTag).sort();
  const totalOps = tags.reduce((n, t) => n + byTag[t].length, 0);

  return (
    <PageBody>
      <PageHeader
        eyebrow="개발자"
        title="API 문서"
        description={
          spec?.info?.description ||
          "DBOps REST API. 모든 경로는 Cognito JWT(Authorization: Bearer)가 필요합니다 — Slack 웹훅(HMAC)과 /health 제외."
        }
      />
      {err && (
        <div className="text-xs text-rose-300 border border-rose-500/40 bg-rose-500/10 px-3 py-2">
          스펙 로드 실패: {err} — /openapi.json 이 배포됐는지 확인하세요.
        </div>
      )}
      {!spec && !err && (
        <div className="text-zinc-500 text-sm">불러오는 중…</div>
      )}
      {spec && tags.length === 0 && (
        <EmptyState
          title="엔드포인트 없음"
          description="openapi.json에 경로가 없습니다."
        />
      )}
      {spec && tags.length > 0 && (
        <div className="text-[11px] text-zinc-500 font-mono">
          {tags.length} groups · {totalOps} endpoints · v{spec.info?.version}
        </div>
      )}
      {tags.map((tag) => (
        <Section
          key={tag}
          eyebrow={`${byTag[tag].length} endpoints`}
          title={tag}
        >
          <div className="border border-zinc-800 bg-zinc-900/40 divide-y divide-zinc-800">
            {byTag[tag].map(({ path, method, op }) => (
              <div
                key={`${method}:${path}`}
                className="flex items-center gap-3 px-3 py-2 flex-wrap"
              >
                <span
                  className={`text-[10px] font-mono uppercase px-1.5 py-0.5 border rounded shrink-0 ${
                    METHOD_STYLES[method] ||
                    "bg-zinc-700/40 text-zinc-300 border-zinc-700"
                  }`}
                >
                  {method}
                </span>
                <span className="font-mono text-xs text-zinc-200 break-all">
                  {path}
                </span>
                {op.parameters && op.parameters.length > 0 && (
                  <span className="text-[10px] text-zinc-500 font-mono">
                    params: {op.parameters.map((p) => p.name).join(", ")}
                  </span>
                )}
                <span className="ml-auto text-[10px] shrink-0">
                  {op.security ? (
                    <span
                      className="text-amber-400/80"
                      title="Cognito JWT 필요"
                    >
                      🔒 JWT
                    </span>
                  ) : (
                    <span
                      className="text-zinc-600"
                      title="공개 (Slack HMAC 또는 health probe)"
                    >
                      public
                    </span>
                  )}
                </span>
              </div>
            ))}
          </div>
        </Section>
      ))}
    </PageBody>
  );
}
