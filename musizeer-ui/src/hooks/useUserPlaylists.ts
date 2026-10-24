import { useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { getUserPlaylists } from "@/lib/api-client";
import { classifyError } from "@/lib/api-errors";
import { useErrorText } from "@/lib/error-text";
import { queryKeys } from "@/lib/query-keys";
import { useCan } from "@/hooks/useCapabilities";
import { useI18n } from "@/i18n";

/** The capability key (sdk/src/musizeer_sdk/capabilities.py) of a metadata provider that shares the tracks of the viewer's own playlists only. */
export const PLAYLIST_TRACKS_RESTRICTED = "playlist_tracks_restricted";

/**
 * The viewer's playlists (own and followed) and the most tracks one may
 * hold. Not asked of a viewer without playlists; `enabled` lets a menu ask
 * only when it opens.
 */
export function useUserPlaylists(enabled = true) {
  const can = useCan("playlists");
  return useQuery({ queryKey: queryKeys.userPlaylists, queryFn: () => getUserPlaylists(), enabled: can && enabled });
}

/** Marks the list of playlists, and each one given, stale: they are read again where they are shown. */
export function useRefreshPlaylists() {
  const queryClient = useQueryClient();
  return useCallback(
    (...ids: string[]) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.userPlaylists });
      for (const id of ids) void queryClient.invalidateQueries({ queryKey: queryKeys.userPlaylist(id) });
    },
    [queryClient],
  );
}

/**
 * Says in a toast why a playlist call failed: the cap in numbers for a full
 * playlist, the backend's code in the viewer's language otherwise, `fallback`
 * when it sent none.
 */
export function usePlaylistFailure() {
  const { tx } = useI18n();
  const errorText = useErrorText();
  return useCallback(
    (error: unknown, fallback: string) => {
      const { body } = classifyError(error);
      if (body?.error_code === "playlist_full") {
        toast.error(tx("error_codes.playlist_full", fallback, { max: body.max_tracks ?? 0 }));
        return;
      }
      toast.error(errorText(body?.error_code, fallback));
    },
    [tx, errorText],
  );
}
