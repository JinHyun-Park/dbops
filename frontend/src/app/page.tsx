import Link from "next/link";

const features = [
  { name: "Chat", href: "/chat", description: "AI와 자연어로 DB 성능 분석", icon: "💬" },
  { name: "Dashboard", href: "/dashboard", description: "클러스터 모니터링 대시보드", icon: "📊" },
  { name: "Query Lab", href: "/query-lab", description: "SQL 분석 및 EXPLAIN", icon: "🔍" },
  { name: "Reports", href: "/reports", description: "자동 생성 성능 리포트", icon: "📋" },
  { name: "Approvals", href: "/approvals", description: "변경 작업 승인 센터", icon: "✅" },
  { name: "Clusters", href: "/clusters", description: "Aurora 클러스터 관리", icon: "🗄️" },
];

export default function HomePage() {
  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100 flex flex-col items-center justify-center p-6">
      <div className="max-w-4xl w-full text-center mb-12">
        <h1 className="text-4xl font-bold mb-3">DBOps</h1>
        <p className="text-lg text-zinc-400">AI-Powered Database Operations for Aurora MySQL/PostgreSQL</p>
        <p className="text-sm text-zinc-500 mt-2">⌘K를 눌러 빠르게 이동하세요</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 max-w-4xl w-full">
        {features.map((f) => (
          <Link
            key={f.name}
            href={f.href}
            className="bg-zinc-800 border border-zinc-700 rounded-lg p-6 hover:border-blue-500 transition-colors group"
          >
            <div className="text-2xl mb-3">{f.icon}</div>
            <div className="text-lg font-semibold text-zinc-100 group-hover:text-blue-400 transition-colors">{f.name}</div>
            <div className="text-sm text-zinc-400 mt-1">{f.description}</div>
          </Link>
        ))}
      </div>
    </div>
  );
}
