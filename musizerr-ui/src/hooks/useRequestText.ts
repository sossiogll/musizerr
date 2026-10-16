import { useCallback, useMemo } from "react";
import { useI18n } from "@/i18n";
import type { DownloadRequest, RequestKind, RequestMode, RequestReason, RequestStatus } from "@/types";

/**
 * The words of a download request and its policy, in the viewer's language:
 * its kind, its state, the reason the policy gave, and a rule's mode. A value a
 * newer server adds shows as the code the server sent until the dictionary
 * has it.
 */
export function useRequestText() {
  const { t, tx } = useI18n();
  const kind = useCallback((value: RequestKind) => tx(`approval.kind.${value}`, value), [tx]);
  const status = useCallback((value: RequestStatus) => tx(`approval.status.${value}`, value), [tx]);
  const reason = useCallback((value: RequestReason) => tx(`approval.reason.${value}`, value), [tx]);
  const mode = useCallback((value: RequestMode) => tx(`approval.mode.${value}`, value), [tx]);
  /** A rule in one phrase: "Automatic up to 30 tracks". */
  const rule = useCallback(
    (value: { mode: RequestMode; threshold: number }) => (value.mode === "auto_up_to" ? t("approval.modeUpTo", { count: value.threshold }) : mode(value.mode)),
    [t, mode],
  );
  /** What a request is called: its title, or its kind when it has none (the likes download). */
  const name = useCallback((request: Pick<DownloadRequest, "kind" | "title">) => request.title || kind(request.kind), [kind]);
  return useMemo(() => ({ kind, status, reason, mode, rule, name }), [kind, status, reason, mode, rule, name]);
}
