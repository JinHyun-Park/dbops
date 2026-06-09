"use client";

import { ClusterDropdown } from "@/components/design-system/cluster-dropdown";

// Drop-in replacement for the per-page flat <select> of clusters. Now a real
// dropdown (ClusterDropdown) that lists clusters immediately with a severity
// dot and typeahead — no longer the ⌘K search palette (which is pages/search
// only again). The `selected` prop is kept for call-site compatibility; the
// dropdown reads the active cluster from the shared store directly.
export function ClusterPicker({
  selected: _selected,
  className = "",
}: {
  selected: string | null;
  className?: string;
}) {
  return <ClusterDropdown className={className} />;
}
