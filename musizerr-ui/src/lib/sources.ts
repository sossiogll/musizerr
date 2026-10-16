import type { QueryClient } from "@tanstack/react-query";
import { queryKeys } from "@/lib/query-keys";

/** The first characters of a digest: enough to tell two images apart on screen. */
export function shortDigest(digest: string): string {
  return digest.replace(/^sha256:/, "").slice(0, 12);
}

/**
 * After the providers or the sources changed: everything that shows them is stale: the sources, the
 * catalogs, what is installed, the gateway's state, and the provider lists (labels, settings) that
 * depend on which providers the registry holds.
 */
export function invalidateProviders(queryClient: QueryClient): Promise<unknown> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.sources }),
    queryClient.invalidateQueries({ queryKey: queryKeys.installedProviders }),
    queryClient.invalidateQueries({ queryKey: queryKeys.providerGateway }),
    queryClient.invalidateQueries({ queryKey: ["providers"] }),
    queryClient.invalidateQueries({ queryKey: queryKeys.providersPublic }),
  ]);
}
