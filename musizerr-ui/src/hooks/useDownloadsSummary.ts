import { useQuery } from "@tanstack/react-query";
import { getDownloadsSummary } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useCan } from "@/hooks/useCapabilities";

/**
 * Download counters (GET /downloads/summary) for the nav badge, the Home
 * status cards and the Downloads tabs. Refreshed by the progress stream
 * (QueueContext invalidates it on every event); only for viewers who can
 * download.
 */
export function useDownloadsSummary() {
  const canDownload = useCan("download");
  return useQuery({
    queryKey: queryKeys.downloadsSummary,
    queryFn: () => getDownloadsSummary(7),
    enabled: canDownload,
    staleTime: 30 * 1000,
  });
}

/** Tracks waiting or being downloaded right now. */
export function useActiveDownloadCount(): number {
  const { data } = useDownloadsSummary();
  return data ? data.phases.queued + data.phases.active : 0;
}
