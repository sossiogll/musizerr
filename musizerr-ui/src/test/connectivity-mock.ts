import { vi } from "vitest";
import type { ConnectivityState } from "@/lib/connectivity";

/**
 * A controllable stand-in for lib/connectivity in component tests:
 * `vi.mock("@/lib/connectivity", () => connectivityMock.module)` then
 * `connectivityMock.set({ online: false })`.
 */
const listeners = new Set<() => void>();
let state: ConnectivityState = { online: true, checking: false, lastOnlineAt: null, nextCheckAt: null };

export const connectivityMock = {
  set(next: Partial<ConnectivityState>) {
    state = { ...state, ...next };
    listeners.forEach((l) => l());
  },
  reset() {
    state = { online: true, checking: false, lastOnlineAt: null, nextCheckAt: null };
  },
  checkNow: vi.fn(async () => state.online),
};

export async function connectivityModule() {
  const { useSyncExternalStore } = await import("react");
  return {
    checkNow: connectivityMock.checkNow,
    reportUnreachable: vi.fn(),
    startConnectivityMonitor: vi.fn(() => () => {}),
    getConnectivity: () => state,
    useConnectivity: () =>
      useSyncExternalStore(
        (l: () => void) => {
          listeners.add(l);
          return () => listeners.delete(l);
        },
        () => state,
      ),
  };
}
