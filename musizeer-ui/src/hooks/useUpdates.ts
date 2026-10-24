import { useQuery } from "@tanstack/react-query";
import { getUpdates } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useCan } from "@/hooks/useCapabilities";

/**
 * The version that runs and the newer releases of Musizeer (GET /admin/updates, ADR-0007 §5.1): only for
 * the viewers who administer. The server checks once a day; an hour of staleness here is plenty.
 */
export function useUpdates() {
  const canAdmin = useCan("admin");
  return useQuery({
    queryKey: queryKeys.updates,
    queryFn: () => getUpdates(),
    enabled: canAdmin,
    staleTime: 60 * 60 * 1000,
  });
}
