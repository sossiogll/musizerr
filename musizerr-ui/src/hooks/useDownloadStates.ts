import { useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { getDownloadStates, type DownloadStateMeta } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useI18n } from "@/i18n";
import type { DownloadStatus } from "@/types";

export interface DownloadStatesView {
  /** The declared metadata of a state (tone, phase, label), if known. */
  meta: (status: DownloadStatus) => DownloadStateMeta | undefined;
  /** Its label in the viewer's language (the backend's label as fallback). */
  label: (status: DownloadStatus) => string;
  states: DownloadStateMeta[];
}

/**
 * The download-state vocabulary from GET /config/download-states: labels,
 * tones and phases are declared by the backend, never mapped here. Fetched
 * once and kept in the offline copy.
 */
export function useDownloadStates(): DownloadStatesView {
  const { tx } = useI18n();
  const { data } = useQuery({
    queryKey: queryKeys.downloadStates,
    queryFn: getDownloadStates,
    staleTime: Infinity,
  });
  const byKey = useMemo(() => new Map((data ?? []).map((s) => [s.key, s])), [data]);
  const meta = useCallback((status: DownloadStatus) => byKey.get(status), [byKey]);
  const label = useCallback(
    (status: DownloadStatus) => {
      const m = byKey.get(status);
      return m ? tx(m.label_key, m.label) : tx(`download_states.${status}`, status);
    },
    [byKey, tx],
  );
  return useMemo(() => ({ meta, label, states: data ?? [] }), [meta, label, data]);
}
