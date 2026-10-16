import type { ReactNode } from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useConnectivity } from "@/lib/connectivity";
import { FirstLoadScreen, LoadingScreen } from "./FirstLoadScreen";

/**
 * Fail-closed gate in front of the app: nothing is rendered until the viewer
 * is known (sign-in on or off, session restored or not). An unknown state
 * never falls back to "sign-in is off". With the API unreachable (or its
 * status request failing) and no saved copy of the viewer, the first-load
 * screen explains why.
 */
export function AppGate({ children }: { children: ReactNode }) {
  const { ready, authState, statusFailed } = useAuth();
  const { online } = useConnectivity();
  if (ready) return <>{children}</>;
  if (authState === "unknown" && (!online || statusFailed)) return <FirstLoadScreen />;
  return <LoadingScreen />;
}
