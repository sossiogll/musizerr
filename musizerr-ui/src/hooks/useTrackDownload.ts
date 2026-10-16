import { useCallback, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { downloadTrack } from "@/lib/api-client";
import { useLiveState, useQueueActions, type LiveTrackState } from "@/contexts/QueueContext";
import { useDownloadStates } from "@/hooks/useDownloadStates";
import { markTrackListsStale, rememberFavorite } from "@/hooks/useTrackFavorite";
import { useServerAction } from "@/hooks/useServerAction";
import { useCapabilities } from "@/hooks/useCapabilities";
import { useAnnouncePending, useApproval, useWaitingRequest, type Approval } from "@/hooks/useDownloadApproval";
import { useFailureToast } from "@/lib/error-text";
import type { DownloadOptions, DownloadPhase, DownloadStateMeta } from "@/lib/api-client";
import { isPendingApproval } from "@/lib/download-requests";
import { useI18n } from "@/i18n";
import type {
  DownloadRequestResult,
  DownloadResponse,
  DownloadStatus,
  PendingApprovalResponse,
  Track,
  TrackStateFields,
  ViewerCapabilities,
} from "@/types";

/**
 * What the status slot of a track shows: the FILE state, as the server says
 * it, or `awaiting`: no file yet, and the viewer's request for it waits for
 * an admin.
 */
export type SlotState =
  | { kind: "available" }
  | { kind: "awaiting" }
  | { kind: "none" }
  | { kind: "state"; status: DownloadStatus; phase: DownloadPhase; meta: DownloadStateMeta | undefined };

/**
 * The downloads a viewer may start, primary first: `plain` (no favorite,
 * admins and open mode) and `favorite` (added to the favorites when it
 * arrives). A user has only `favorite`: the server forces it anyway.
 */
export type DownloadMode = "plain" | "favorite";

export function downloadModes(can: ViewerCapabilities): DownloadMode[] {
  if (!can.download) return [];
  return can.download_plain ? ["plain", "favorite"] : ["favorite"];
}

export const downloadOptions = (mode: DownloadMode): DownloadOptions => ({ addToFavorites: mode === "favorite" });

/** The annotated track of a download response (a retry answers without it). */
const answeredTrack = (res: DownloadRequestResult | DownloadResponse): Track | undefined =>
  "track" in res ? res.track : undefined;

/**
 * Whether the download just requested will add the track to the viewer's
 * favorites: the server's answer (the response's track is annotated like
 * every list), else the option sent (a retry answers without the track).
 */
export function favoriteOnArrival(res: DownloadRequestResult | DownloadResponse, mode: DownloadMode): boolean {
  return answeredTrack(res)?.favorite_pending ?? mode === "favorite";
}

export interface TrackDownload {
  slot: SlotState;
  /** The downloads this viewer may start, primary first (empty: none). */
  modes: DownloadMode[];
  /** Start (or retry) the download of this track. */
  download: (mode: DownloadMode) => void;
  busy: boolean;
  /** Why the action is disabled (e.g. offline), if it is. */
  disabledReason: string | undefined;
  /** A download started here will add the track to the favorites. */
  favoriteQueued: boolean;
  /** The track's id in the library database: from the list, or from its live progress. */
  libraryId: string | null;
  /** Whether a download started here would wait for an admin, and what to tell about it. */
  approval: Approval;
}

/**
 * What the status slot shows, from what the server said:
 *
 * 1. `server` (the list's file state) `downloaded`: the track is in the
 *    library, and no attempt takes it out — a newer attempt (an admin's forced
 *    download) or a live "failed" never turns it into "Delegated" or "Retry".
 * 2. Otherwise the live event, newer than the list: its `file_state` (the
 *    server's decision at that instant), or its status from an older server.
 * 3. Otherwise the optimistic "pending" of a download just started here.
 * 4. Otherwise the list's state.
 */
export function slotStatus(
  server: DownloadStatus | null | undefined,
  live: Pick<LiveTrackState, "status" | "file_state"> | undefined,
  queued: boolean,
): DownloadStatus | null {
  if (server === "downloaded") return "downloaded";
  const liveState = live ? (live.file_state ?? live.status) : null;
  if (liveState) return liveState;
  if (queued) return "pending";
  return server ?? null;
}

/**
 * The file state of one track (``slotStatus``) and the action to (re)start
 * its download. Every state comes from the server: nothing is derived from
 * other fields (a stream URL says nothing about the library).
 */
export function useTrackDownload(
  track: TrackStateFields,
  statusOverride?: DownloadStatus | null,
  /** Retry through another endpoint (e.g. POST /downloads/{id}/retry) instead of a new download. */
  retry?: (options: DownloadOptions) => Promise<DownloadRequestResult | DownloadResponse>,
): TrackDownload {
  const { can } = useCapabilities();
  const modes = downloadModes(can);
  const server = useServerAction();
  const states = useDownloadStates();
  const addItems = useQueueActions();
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const announcePending = useAnnouncePending();
  const failed = useFailureToast();
  const waiting = useWaitingRequest("track", track.provider, track.provider_id);
  const approval = useApproval("track", 1);
  const [queued, setQueued] = useState<{ id: string; favorite: boolean } | null>(null);
  // The server answered that the track is already in the library: its id.
  const [owned, setOwned] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const live = useLiveState(track.library_id ?? owned ?? track.internal_id, queued?.id);

  const listed = owned ? "downloaded" : (statusOverride ?? track.download_status);
  const status = slotStatus(listed, live, !!queued);

  const slotPhaseRef = useRef<DownloadPhase | null>(null);
  let slot: SlotState;
  if (status) {
    const meta = states.meta(status);
    slot = { kind: "state", status, phase: meta?.phase ?? (status === "downloaded" ? "done" : "queued"), meta };
  } else if (waiting) {
    slot = { kind: "awaiting" };
  } else {
    slot = modes.length > 0 && track.provider && track.provider_id ? { kind: "available" } : { kind: "none" };
  }
  slotPhaseRef.current = slot.kind === "state" ? slot.phase : null;

  const download = useCallback(
    async (mode: DownloadMode) => {
      setBusy(true);
      const options = downloadOptions(mode);
      try {
        const res: DownloadRequestResult | DownloadResponse | PendingApprovalResponse =
          retry && slotPhaseRef.current === "failed"
            ? await retry(options)
            : await downloadTrack(track.provider, track.provider_id, track.external_ids, options);
        if (isPendingApproval(res)) {
          // Nothing was queued and no heart was set: an admin decides (the approval policy).
          announcePending(res);
          return;
        }
        markTrackListsStale(queryClient);
        if (res.status === "already_in_library" || !res.download_id) {
          // The response says whether it is a favorite now (applied at once
          // when asked): the heart reads it instead of asking again.
          const favorite = answeredTrack(res)?.favorite;
          if (favorite !== undefined) rememberFavorite(queryClient, res.library_id, favorite);
          setOwned(res.library_id);
          toast.info(t("common.alreadyInLibrary", { title: track.title }));
          return;
        }
        setQueued({ id: res.download_id, favorite: favoriteOnArrival(res, mode) });
        addItems([res.download_id]);
        toast.success(t("common.trackQueued", { title: track.title }));
      } catch (error) {
        failed(error, t("common.downloadFailed"));
      } finally {
        setBusy(false);
      }
    },
    [track, retry, addItems, queryClient, t, announcePending, failed],
  );

  return {
    slot,
    modes,
    download: (mode) => void download(mode),
    busy,
    disabledReason: server.reason,
    favoriteQueued: !!queued?.favorite,
    libraryId: track.library_id ?? owned ?? live?.track_id ?? null,
    approval,
  };
}
