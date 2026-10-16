import { useCallback } from "react";
import { useIsMutating, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { addFavorite, getFavorite, removeFavorite } from "@/lib/api-client";
import { TRACK_LIST_ROOTS, queryKeys } from "@/lib/query-keys";
import { useCan } from "@/hooks/useCapabilities";
import type { SlotState } from "@/hooks/useTrackDownload";
import { useI18n } from "@/i18n";
import type { TrackStateFields } from "@/types";

/**
 * The heart of a track (a row, the track page, the player):
 * - `hidden`: not in the library (no disabled hearts cluttering lists), or
 *   the viewer has no favorites;
 * - `pending`: a download of the viewer's will add it to the favorites when
 *   it arrives (queued or running; gone if it fails);
 * - `heart`: in the library, liked or not, and the toggle.
 */
export type HeartState =
  | { kind: "hidden" }
  | { kind: "pending" }
  | { kind: "heart"; favorite: boolean; busy: boolean; toggle: () => void };

const IN_FLIGHT = new Set(["queued", "active"]);

interface FavoriteEntry {
  track_id: string;
  favorite: boolean;
  in_library: boolean;
}

/**
 * What the server answered about a library track's favorite: every view of
 * that track reads it (over its list's older `favorite`).
 */
export function rememberFavorite(queryClient: QueryClient, libraryId: string, favorite: boolean): void {
  queryClient.setQueryData<FavoriteEntry>(queryKeys.favorite(libraryId), { track_id: libraryId, favorite, in_library: true });
}

/** The lists that carry library states, stale after the viewer changed one (asked again on their next use). */
export function markTrackListsStale(queryClient: QueryClient): void {
  for (const root of TRACK_LIST_ROOTS) void queryClient.invalidateQueries({ queryKey: [root], refetchType: "none" });
}

const toggleKey = (libraryId: string) => ["favorite-toggle", libraryId] as const;

/**
 * The viewer's favorite for one library track, the same in every view of
 * it: the shared entry once a change was made (here or anywhere else) or
 * the server was asked (`ask`), else what the list said. The toggle writes
 * the server's answer to the shared entry, so a row, the track page and the
 * player change together; `busy` too, while any of them is waiting.
 */
export function useLibraryFavorite(libraryId: string | null, listed: boolean, ask: boolean) {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const key = libraryId ?? "";
  const shared = useQuery({
    queryKey: queryKeys.favorite(key),
    queryFn: () => getFavorite(key),
    enabled: ask && !!libraryId,
    staleTime: Infinity,
  });
  const favorite = shared.data?.favorite ?? listed;

  const change = useMutation({
    mutationKey: toggleKey(key),
    mutationFn: (like: boolean) => (like ? addFavorite(key) : removeFavorite(key)),
    onSuccess: (res) => {
      rememberFavorite(queryClient, key, res.favorite);
      markTrackListsStale(queryClient);
      void queryClient.invalidateQueries({ queryKey: queryKeys.favorites() });
    },
    onError: () => toast.error(t("heart.failed")),
  });
  const busy = useIsMutating({ mutationKey: toggleKey(key) }) > 0;
  const { mutate } = change;
  const toggle = useCallback(() => {
    if (libraryId && !busy) mutate(!favorite);
  }, [libraryId, busy, favorite, mutate]);

  return { favorite, asking: shared.isPending && shared.fetchStatus !== "idle", busy, toggle };
}

export function useTrackFavorite(
  track: TrackStateFields,
  slot: SlotState,
  libraryId: string | null,
  /** A download with favorite was started from this view. */
  favoriteQueued: boolean,
): HeartState {
  const canFavorite = useCan("favorite");

  const inLibrary = slot.kind === "state" && slot.phase === "done";
  const intent = !!track.favorite_pending || favoriteQueued;
  const pending = intent && slot.kind === "state" && IN_FLIGHT.has(slot.phase);
  // Landed while this list was on screen: the list's own `favorite` is from
  // before the arrival, so the view asks the server (which applied the
  // favorite on arrival) instead of guessing.
  const landedHere = inLibrary && !track.in_library && !!libraryId;
  const { favorite, asking, busy, toggle } = useLibraryFavorite(
    libraryId,
    !landedHere && !!track.favorite,
    canFavorite && landedHere,
  );

  if (!canFavorite) return { kind: "hidden" };
  if (inLibrary && libraryId) {
    // Still asking: an intent keeps its pending heart until the answer.
    if (asking && intent) return { kind: "pending" };
    return { kind: "heart", favorite, busy, toggle };
  }
  if (pending) return { kind: "pending" };
  return { kind: "hidden" };
}
