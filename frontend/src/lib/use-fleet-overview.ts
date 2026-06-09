"use client";

// Single shared poll of the multi-cluster overview. The cluster dropdown, the
// dashboard cluster strip, and the incident-summary banner all need per-cluster
// severity — without this they each ran their OWN fetch + 30s interval, so the
// browser hit /api/multi-cluster/overview 3× as often AND the three could show
// momentarily inconsistent severity. One module-level store (useSyncExternal
// store) dedupes the poll and keeps every consumer in lockstep.
import { useSyncExternalStore } from "react";
import { fetchMultiClusterOverview } from "@/lib/api-client";
import type { TriageInput } from "@/lib/cluster-triage";

export interface FleetRow extends TriageInput {
  cluster_id: string;
  engine?: string;
  engine_version?: string;
}

const POLL_MS = 30000;

let rows: FleetRow[] = [];
let started = false;
let timer: ReturnType<typeof setInterval> | null = null;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function load() {
  fetchMultiClusterOverview()
    .then((r: { clusters?: FleetRow[] }) => {
      rows = r.clusters || [];
      emit();
    })
    .catch(() => {
      // Keep the last good snapshot on a transient failure rather than
      // blanking severity — a flap shouldn't make every cluster read "ok".
    });
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  if (!started) {
    started = true;
    load();
    timer = setInterval(load, POLL_MS);
  }
  return () => {
    listeners.delete(cb);
    if (listeners.size === 0 && timer) {
      clearInterval(timer);
      timer = null;
      started = false;
    }
  };
}

// Stable reference between emits so useSyncExternalStore doesn't loop.
function getSnapshot(): FleetRow[] {
  return rows;
}

export function useFleetOverview(): FleetRow[] {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
