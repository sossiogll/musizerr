import { useQuery } from "@tanstack/react-query";
import { getPendingRequestCount } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useCan } from "@/hooks/useCapabilities";

/**
 * How many download requests wait for an admin (GET
 * /admin/download-requests/pending-count): the badge of Administration and of
 * its Requests tab. Kept current by the progress stream (RequestsWatcher
 * writes the count the events carry), so it is never polled; only for the
 * viewers who administer.
 */
export function usePendingRequestCount(): number {
  const canAdmin = useCan("admin");
  const { data } = useQuery({
    queryKey: queryKeys.pendingRequestCount,
    queryFn: () => getPendingRequestCount(),
    enabled: canAdmin,
    staleTime: 60 * 1000,
  });
  return data?.pending ?? 0;
}
