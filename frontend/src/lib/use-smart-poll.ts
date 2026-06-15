/**
 * useSmartPoll — visibility-aware polling hook.
 *
 * Semantics:
 *  - Fires `callback` immediately on mount AND whenever `deps` change.
 *  - While the tab is VISIBLE, re-fires every `activeMs`.
 *  - While HIDDEN, the timer is suspended entirely (no API calls).
 *  - On becoming visible again, fires `callback` immediately (catch-up),
 *    then resumes the interval from that moment.
 *  - Cleans up the interval and event listener on unmount.
 *
 * `deps`: pass the values the callback reads (e.g. the selected cluster /
 * range). When they change, the hook re-fires `callback` IMMEDIATELY and
 * restarts the interval — without this, a callback whose inputs arrive after
 * mount (e.g. a cluster id resolved by an async fetch) would no-op on the
 * mount fire and then sit idle until the next interval tick, leaving the UI
 * "loading" for up to `activeMs`. Defaults to `[]` (mount-only) for callers
 * with no changing inputs.
 *
 * Stability: `callback` and `activeMs` are captured in refs so a new callback
 * identity on every render does NOT re-subscribe the hook; only a change in
 * `deps` does. The latest callback is always used on the next tick.
 *
 * Double-fire guard: the `visibilitychange` → immediate-fire path and the
 * setInterval path share the same `running` ref. The interval is cleared
 * before a new one is created in every code path, so two timers can never
 * coexist.
 */
import { useEffect, useRef } from "react";

export function useSmartPoll(
  callback: () => void | Promise<void>,
  activeMs: number,
  deps: unknown[] = [],
): void {
  // Use refs so the effect body captures stable references — changing
  // `callback` or `activeMs` updates the ref without re-running the effect.
  const callbackRef = useRef(callback);
  const activeMsRef = useRef(activeMs);

  useEffect(() => {
    callbackRef.current = callback;
  }, [callback]);

  useEffect(() => {
    activeMsRef.current = activeMs;
  }, [activeMs]);

  useEffect(() => {
    // The single running interval handle. `null` means "not running".
    let intervalId: ReturnType<typeof setInterval> | null = null;

    function startInterval() {
      if (intervalId !== null) clearInterval(intervalId);
      intervalId = setInterval(() => {
        callbackRef.current();
      }, activeMsRef.current);
    }

    function stopInterval() {
      if (intervalId !== null) {
        clearInterval(intervalId);
        intervalId = null;
      }
    }

    function handleVisibilityChange() {
      if (document.visibilityState === "hidden") {
        stopInterval();
      } else {
        // Tab became visible — fire immediately to catch up, then restart
        // the interval from this moment (avoids an awkward leading delay).
        callbackRef.current();
        startInterval();
      }
    }

    // Fire immediately on mount, then start the interval.
    callbackRef.current();
    if (document.visibilityState !== "hidden") {
      startInterval();
    }

    document.addEventListener("visibilitychange", handleVisibilityChange);

    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      stopInterval();
    };
    // Re-run (re-fire + restart) whenever `deps` change. `callback`/`activeMs`
    // identity is intentionally excluded — they flow through the refs above so
    // a new callback closure every render doesn't tear down the listener; only
    // a real input change (deps) should re-fire.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
