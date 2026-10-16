import { useConnectivity } from "@/lib/connectivity";
import { useI18n } from "@/i18n";

export interface ServerActionState {
  disabled: boolean;
  /** Why the action is disabled, to show next to it (title / aria-description). */
  reason: string | undefined;
}

/**
 * An action that needs the API (download, retry, sync, save): disabled with
 * a reason while the API is unreachable. Permissions are not decided here —
 * they come from the viewer's capabilities (useCapabilities).
 */
export function useServerAction(): ServerActionState {
  const { online } = useConnectivity();
  const { t } = useI18n();
  return online ? { disabled: false, reason: undefined } : { disabled: true, reason: t("connectivity.needsServer") };
}
