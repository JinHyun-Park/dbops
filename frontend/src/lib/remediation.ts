// Wilson score lower bound at 95% — so 1/1 doesn't outrank 9/10. Pure + testable.
export function confidence(successes: number, attempts: number): number {
  if (attempts <= 0) return 0;
  const z = 1.96;
  const p = successes / attempts;
  const denom = 1 + (z * z) / attempts;
  const centre = p + (z * z) / (2 * attempts);
  const margin =
    z * Math.sqrt((p * (1 - p) + (z * z) / (4 * attempts)) / attempts);
  return Math.max(0, (centre - margin) / denom);
}

export function trackRecordLabel(successes: number, attempts: number): string {
  if (attempts <= 0) return "이력 없음";
  return `${successes}/${attempts}회 해결`;
}
