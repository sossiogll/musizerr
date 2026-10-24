import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import type { ExternalId } from "@/types";
import { getAlternativeProviders } from "@/lib/enrichment";

interface EnrichedFetchOptions<T extends { external_ids: ExternalId[] }> {
  queryKey: unknown[];
  provider: string;
  id: string;
  stateItem: T | null | undefined;
  fetchFn: (provider: string, id: string) => Promise<T>;
  mergeWithState: (fetched: T, state: T) => T;
}

/**
 * Hook generico per fetchare un'entità con fallback cross-provider.
 *
 * Comportamento:
 * 1. Mostra stateItem immediatamente come placeholder.
 * 2. Fetcha dal provider primario (URL params).
 * 3. Se fallisce, prova i provider alternativi estratti da stateItem.external_ids.
 * 4. Se tutti falliscono ma stateItem esiste, lo restituisce — la pagina
 *    non mostra mai "not found" per un item che l'utente ha appena navigato.
 * 5. Su successo, esegue mergeWithState per unire external_ids e campi nullable
 *    dal stateItem nel risultato fetchato.
 */
export function useEnrichedFetch<T extends { external_ids: ExternalId[] }>({
  queryKey,
  provider,
  id,
  stateItem,
  fetchFn,
  mergeWithState,
}: EnrichedFetchOptions<T>): UseQueryResult<T> {
  return useQuery({
    queryKey,
    queryFn: async () => {
      let lastError: unknown;

      // 1. Prova provider primario
      try {
        return await fetchFn(provider, id);
      } catch (err) {
        lastError = err;
      }

      // 2. Prova provider alternativi da stateItem.external_ids
      if (stateItem?.external_ids?.length) {
        for (const alt of getAlternativeProviders(stateItem.external_ids, provider)) {
          try {
            return await fetchFn(alt.provider, alt.id);
          } catch {
            // continua col prossimo
          }
        }
      }

      // 3. Tutti falliti — restituisce stateItem per non mostrare "not found"
      if (stateItem) return stateItem;
      throw lastError;
    },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    placeholderData: (stateItem ?? undefined) as any,
    select: (fetched) => {
      if (!stateItem) return fetched;
      return mergeWithState(fetched, stateItem);
    },
  });
}
