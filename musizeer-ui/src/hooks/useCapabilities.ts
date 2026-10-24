import { useMemo } from "react";
import { useAuth } from "@/contexts/AuthContext";
import type { ViewerCapabilities, ViewerMode } from "@/types";

export type Capability = keyof ViewerCapabilities;

const NONE: ViewerCapabilities = {
  search: false,
  download: false,
  download_plain: false,
  favorite: false,
  stream_full: false,
  library: false,
  playlists: false,
  settings_personal: false,
  linked_accounts: false,
  admin: false,
};

export interface Capabilities {
  /** null while the viewer is unknown. */
  mode: ViewerMode | null;
  can: ViewerCapabilities;
}

/**
 * What the current viewer may do, exactly as GET /auth/account/status says
 * (computed on the server, services/capabilities.py). The ONLY source of
 * permissions in the UI: never re-derive them from auth_enabled, the user's
 * role or a token. Fail-closed: nothing is allowed while it is unknown.
 * The server still enforces every rule; this only decides what to show.
 */
export function useCapabilities(): Capabilities {
  const { authConfig } = useAuth();
  return useMemo(
    () => ({ mode: authConfig?.mode ?? null, can: authConfig?.capabilities ?? NONE }),
    [authConfig],
  );
}

export function useCan(capability: Capability): boolean {
  return useCapabilities().can[capability];
}
