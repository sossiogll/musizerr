import { useSyncExternalStore } from "react";
import axios from "axios";
import { onlineManager } from "@tanstack/react-query";

/**
 * Is the API reachable? Decided by GET /health, never by the browser's own
 * online flag alone (the network can be up while the server is down).
 *
 * - A request that fails as `unreachable` (see api-errors.ts) reports it
 *   here; the monitor checks /health at once.
 * - One failed check is not an outage: a /health request can time out only
 *   because it waited behind other requests for one of the browser's few
 *   connections to the origin. The API is declared down after
 *   OFFLINE_AFTER_FAILURES consecutive failures, confirmed CONFIRM_DELAY_MS
 *   apart; one success in between clears them.
 * - While the API is down /health is polled with exponential backoff
 *   (BASE → MAX); while it is up, a slow heartbeat notices an idle outage.
 * - The state drives TanStack Query's `onlineManager`: queries pause while
 *   offline and refetch on their own when the API is back.
 */

export const HEALTH_URL = "/api/health";
export const HEALTH_TIMEOUT_MS = 5_000;
export const OFFLINE_AFTER_FAILURES = 3;
export const CONFIRM_DELAY_MS = 1_000;
export const RETRY_BASE_MS = 2_000;
export const RETRY_MAX_MS = 30_000;
export const HEARTBEAT_MS = 60_000;

export interface ConnectivityState {
  online: boolean;
  /** A /health request is in flight. */
  checking: boolean;
  /** When the API last answered /health (ms epoch), null if never. */
  lastOnlineAt: number | null;
  /** When the next automatic check runs while offline (ms epoch). */
  nextCheckAt: number | null;
}

let state: ConnectivityState = { online: true, checking: false, lastOnlineAt: null, nextCheckAt: null };
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setTimeout> | undefined;
let failures = 0;
let inFlight: Promise<boolean> | null = null;
let started = false;

function set(next: Partial<ConnectivityState>) {
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

function schedule(delay: number) {
  clearTimeout(timer);
  timer = setTimeout(() => void checkNow(), delay);
  set({ nextCheckAt: state.online ? null : Date.now() + delay });
}

export function backoffDelay(attempt: number): number {
  return Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** Math.max(0, attempt - 1));
}

/** Checks /health now (one request at a time). Resolves to the new state. */
export function checkNow(): Promise<boolean> {
  if (inFlight) return inFlight;
  set({ checking: true });
  inFlight = axios
    .get(HEALTH_URL, { timeout: HEALTH_TIMEOUT_MS })
    .then(() => true, () => false)
    .then((ok) => {
      inFlight = null;
      if (ok) {
        failures = 0;
        set({ online: true, checking: false, lastOnlineAt: Date.now(), nextCheckAt: null });
        schedule(HEARTBEAT_MS);
      } else {
        failures += 1;
        if (state.online && failures < OFFLINE_AFTER_FAILURES) {
          // Not yet: confirm with another check before calling it an outage.
          set({ checking: false });
          schedule(CONFIRM_DELAY_MS);
        } else {
          set({ online: false, checking: false });
          schedule(backoffDelay(Math.max(1, failures - OFFLINE_AFTER_FAILURES + 1)));
        }
      }
      return ok;
    });
  return inFlight;
}

/** A request could not reach the API: verify with /health. */
export function reportUnreachable(): void {
  if (!started) return;
  void checkNow();
}

export function getConnectivity(): ConnectivityState {
  return state;
}

export function subscribeConnectivity(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useConnectivity(): ConnectivityState {
  return useSyncExternalStore(subscribeConnectivity, getConnectivity, getConnectivity);
}

/**
 * Starts the monitor once and hands TanStack Query's online state to it.
 * Returns a stop function (used by tests).
 */
export function startConnectivityMonitor(): () => void {
  if (started) return stopConnectivityMonitor;
  started = true;
  onlineManager.setEventListener((setOnline) => {
    setOnline(state.online);
    return subscribeConnectivity(() => setOnline(state.online));
  });
  const onBrowserOffline = () => {
    set({ online: false });
    schedule(backoffDelay(Math.max(1, failures)));
  };
  const onBrowserOnline = () => void checkNow();
  window.addEventListener("offline", onBrowserOffline);
  window.addEventListener("online", onBrowserOnline);
  stopListeners = () => {
    window.removeEventListener("offline", onBrowserOffline);
    window.removeEventListener("online", onBrowserOnline);
  };
  void checkNow();
  return stopConnectivityMonitor;
}

let stopListeners = () => {};

export function stopConnectivityMonitor(): void {
  clearTimeout(timer);
  stopListeners();
  stopListeners = () => {};
  started = false;
  failures = 0;
  inFlight = null;
  state = { online: true, checking: false, lastOnlineAt: null, nextCheckAt: null };
  for (const listener of listeners) listener();
}
