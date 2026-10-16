import type { ExternalId } from "@/types";

/**
 * Da external_ids, restituisce coppie {provider, id} alternative
 * (escludendo il provider già in uso).
 */
export function getAlternativeProviders(
  externalIds: ExternalId[],
  currentProvider: string,
): { provider: string; id: string }[] {
  return externalIds
    .filter((identifier) => identifier.provider && identifier.provider !== currentProvider)
    .map((identifier) => ({ provider: identifier.provider!, id: identifier.value }));
}

/**
 * Sceglie il miglior {provider, id} per la navigazione
 * preferendo i provider in ordine di priorità.
 * Usato in artist.tsx per i link agli album: preferisce provider con pagina
 * album navigabile su MBID (i MBID di get_artist_albums sono release-group,
 * non release).
 */
export function getPreferredLink(
  item: { provider: string; provider_id: string; external_ids: ExternalId[] },
  priority: string[],
): { provider: string; id: string } {
  for (const preferred of priority) {
    if (item.provider === preferred) {
      return { provider: item.provider, id: item.provider_id };
    }
    const externalId = item.external_ids.find((identifier) => identifier.provider === preferred);
    if (externalId) {
      return { provider: preferred, id: externalId.value };
    }
  }
  return { provider: item.provider, id: item.provider_id };
}

/**
 * Unisce external_ids di state in quelli di fetched, deduplicando.
 */
export function mergeExternalIds(
  fetched: ExternalId[],
  state: ExternalId[],
): ExternalId[] {
  const keyOf = (identifier: ExternalId) =>
    `${identifier.type}:${identifier.provider ?? ""}:${identifier.value}`;
  const seen = new Set(fetched.map(keyOf));
  return [...fetched, ...state.filter((identifier) => !seen.has(keyOf(identifier)))];
}
