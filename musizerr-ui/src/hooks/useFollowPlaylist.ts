import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { deleteUserPlaylist, followPlaylist } from "@/lib/api-client";
import { useCan } from "@/hooks/useCapabilities";
import { useAnnouncePending } from "@/hooks/useDownloadApproval";
import { usePlaylistFailure, useRefreshPlaylists, useUserPlaylists } from "@/hooks/useUserPlaylists";
import { useI18n } from "@/i18n";
import type { PlaylistDownload } from "@/types";

/**
 * Following a playlist of a metadata provider, and unfollowing it: what the
 * "Follow" button of its page and the "Follow" entry of its menu both do.
 * Following keeps the playlist among the viewer's own (read-only) and asks to
 * download its tracks through the approval policy; they do not become
 * favorites. `enabled` false does not even ask for the viewer's playlists
 * (a menu that is not about a playlist).
 */
export function useFollowPlaylist({ provider, id, name }: { provider: string; id: string; name: string }, enabled = true) {
  const { t } = useI18n();
  const can = useCan("playlists");
  const failure = usePlaylistFailure();
  const refresh = useRefreshPlaylists();
  const announcePending = useAnnouncePending();
  const { data } = useUserPlaylists(enabled);
  const followed = data?.items.find((p) => p.kind === "followed" && p.source_provider === provider && p.source_id === id);

  function announceDownload(download: PlaylistDownload, playlistName: string) {
    if (download.status === "pending_approval" && download.needed_tracks !== undefined && download.reason) {
      announcePending({ needed_tracks: download.needed_tracks, reason: download.reason });
    } else if (download.status === "queued" && download.queued) {
      toast.success(t("playlists.followedQueued", { name: playlistName, count: download.queued }));
    } else if (download.status === "error") {
      toast.warning(t("playlists.followedDownloadError", { name: playlistName }));
    } else {
      toast.success(t("playlists.followedNothing", { name: playlistName }));
    }
  }

  const follow = useMutation({
    mutationFn: () => followPlaylist(provider, id),
    onSuccess: (result) => {
      refresh();
      if (!result.created) return;
      announceDownload(result.download, result.playlist.name);
    },
    onError: (error) => failure(error, t("playlists.followFailed")),
  });

  const unfollow = useMutation({
    mutationFn: (deleteTracks: boolean) => deleteUserPlaylist(followed!.id, { deleteTracks }),
    onSuccess: (outcome) => {
      refresh(followed!.id);
      toast.success(t("playlists.unfollowed", { name }));
      if (outcome.tracks_scheduled > 0) toast.info(t("playlists.deletingTracks", { count: outcome.tracks_scheduled }));
    },
    onError: (error) => failure(error, t("playlists.deleteFailed")),
  });

  return { can, followed, follow, unfollow };
}
