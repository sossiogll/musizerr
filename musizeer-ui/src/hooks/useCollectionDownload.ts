import { useCallback, useState } from "react";
import { toast } from "sonner";
import { downloadAlbum, downloadArtist, downloadPlaylist } from "@/lib/api-client";
import { useQueueActions } from "@/contexts/QueueContext";
import { useAnnouncePending } from "@/hooks/useDownloadApproval";
import { downloadOptions, type DownloadMode } from "@/hooks/useTrackDownload";
import { isPendingApproval } from "@/lib/download-requests";
import { useFailureToast } from "@/lib/error-text";
import { useI18n } from "@/i18n";
import type { ItemKind } from "@/types";

const DOWNLOADERS = { album: downloadAlbum, artist: downloadArtist, playlist: downloadPlaylist } as const;

export type CollectionKind = Exclude<ItemKind, "track">;

/**
 * Queues every track of an album, a playlist or an artist — or, with
 * `onlyMissing` (album, playlist), only those not in the library yet. The
 * mode says whether they join the favorites when they arrive (a user's
 * always do: the server forces it). When the approval policy makes the
 * request wait, nothing is queued: it says the request was sent.
 */
export function useCollectionDownload() {
  const addItems = useQueueActions();
  const { t } = useI18n();
  const announcePending = useAnnouncePending();
  const failed = useFailureToast();
  const [busy, setBusy] = useState(false);
  const download = useCallback(
    async (kind: CollectionKind, provider: string, id: string, mode: DownloadMode, options: { onlyMissing?: boolean } = {}) => {
      setBusy(true);
      try {
        const res = await DOWNLOADERS[kind](provider, id, { onlyMissing: options.onlyMissing ?? false, ...downloadOptions(mode) });
        if (isPendingApproval(res)) {
          // Nothing was queued and no heart was set: an admin decides (the approval policy).
          announcePending(res);
        } else {
          addItems(res.download_ids);
          toast.success(
            res.total_tracks === 0 && res.skipped
              ? t("common.nothingMissing")
              : t("common.queuedTracks", { count: res.total_tracks }),
          );
        }
        // A discography is gathered release by release: say when some could not be read.
        if (res.releases_failed) toast.warning(t("common.releasesFailed", { count: res.releases_failed }));
      } catch (error) {
        failed(error, t("common.downloadFailed"));
      } finally {
        setBusy(false);
      }
    },
    [addItems, t, announcePending, failed],
  );
  return { download, busy };
}
