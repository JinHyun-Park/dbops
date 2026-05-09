const API_BASE = process.env.NEXT_PUBLIC_API_URL || "https://vp8z6cdxcd.execute-api.ap-northeast-2.amazonaws.com";

export async function fetchDashboard(clusterId: string) {
  const res = await fetch(`${API_BASE}/api/dashboard/${clusterId}`);
  if (!res.ok) throw new Error(`Dashboard fetch failed: ${res.status}`);
  return res.json();
}

export async function fetchClusters() {
  const res = await fetch(`${API_BASE}/api/clusters`);
  if (!res.ok) throw new Error(`Clusters fetch failed: ${res.status}`);
  return res.json();
}

export async function registerCluster(data: {
  cluster_id: string;
  account_id: string;
  region: string;
  engine?: string;
  spoke_role_arn?: string;
}) {
  const res = await fetch(`${API_BASE}/api/clusters`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Register failed: ${res.status}`);
  return res.json();
}
