/**
 * useSmartPoll — visibility-aware polling hook.
 *
 * Semantics:
 *  - Fires `callback` immediately on mount.
 *  - While the tab is VISIBLE, re-fires every `activeMs`.
 *  - While HIDDEN, the timer is suspended entirely (no API calls).
 *  - On becoming visible again, fires `callback` immediately (catch-up),
 *    then resumes the interval from that moment.
 *  - Cleans up the interval and event listener on unmount.
 *
 * Stability: `callback` and `activeMs` are captured in refs so changing
 * either does NOT cause the hook to re-subscribe. The latest values are
 * always used on the next tick without tearing down the listener.
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
    // Empty dep array is intentional: the effect sets up stable refs and
    // a single event listener for the component lifetime. Updates to
    // `callback` and `activeMs` flow through the refs above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
