import type { QueryClient } from "@tanstack/react-query";
import type { Track } from "@/types";

/** Deep enough for every list the API answers (items, groups, album tracks). */
const MAX_DEPTH = 5;

function findIn(value: unknown, provider: string, providerId: string, depth: number): Track | null {
  if (depth > MAX_DEPTH || !value || typeof value !== "object") return null;
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findIn(item, provider, providerId, depth + 1);
      if (found) return found;
    }
    return null;
  }
  const record = value as Record<string, unknown>;
  if (record.provider === provider && record.provider_id === providerId && "playback" in record) return record as unknown as Track;
  for (const item of Object.values(record)) {
    const found = findIn(item, provider, providerId, depth + 1);
    if (found) return found;
  }
  return null;
}

/**
 * The track as the server describes it NOW: the lists on screen are asked
 * again, then the one with the same provider and id is looked up in their
 * answers. For a track whose audio URL expired while it waited in the queue.
 */
export async function refetchTrack(
  queryClient: QueryClient,
  { provider, provider_id }: { provider: string; provider_id: string },
): Promise<Track | null> {
  await queryClient.invalidateQueries({ type: "active" });
  for (const query of queryClient.getQueryCache().findAll({ type: "active" })) {
    const found = findIn(query.state.data, provider, provider_id, 0);
    if (found) return found;
  }
  return null;
}
