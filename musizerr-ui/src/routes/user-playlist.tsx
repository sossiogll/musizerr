import { useId, useMemo } from "react";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ListMusic, Lock, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";
import {
  getUserPlaylist,
  refreshUserPlaylist,
  removePlaylistTrack,
  reorderPlaylistTracks,
  updateUserPlaylist,
} from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { absoluteTime } from "@/lib/dates";
import { isPlayable, toPlayerTrack, usePlayer } from "@/contexts/PlayerContext";
import { useAnnouncePending } from "@/hooks/useDownloadApproval";
import { useServerAction } from "@/hooks/useServerAction";
import { usePlaylistFailure, useRefreshPlaylists, useUserPlaylists } from "@/hooks/useUserPlaylists";
import { useErrorText } from "@/lib/error-text";
import { useI18n } from "@/i18n";
import { BackButton } from "@/components/BackButton";
import { EmptyState } from "@/components/ContentList";
import { DetailError } from "@/components/DetailError";
import { DetailHeader } from "@/components/DetailHeader";
import { useProviderLabel } from "@/components/ProviderBadges";
import { UserPlaylistMenu } from "@/components/menu/UserPlaylistMenu";
import { PlaylistTrackList } from "@/components/playlists/PlaylistTrackList";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import type { PlaylistDownload, UserPlaylistDetail } from "@/types";

/** One playlist of the viewer: its tracks in order, edited when it is their own, followed from its source otherwise. */
export function UserPlaylistPage() {
  const { id } = useParams({ from: "/playlists/$id" });
  const { t } = useI18n();
  const { data: playlist, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: queryKeys.userPlaylist(id),
    queryFn: () => getUserPlaylist(id),
  });

  if (isLoading) return <Skeleton className="h-56 rounded-lg" />;
  if (!playlist) return <DetailError error={error} icon={ListMusic} notFound={t("playlists.notFound")} onRetry={() => void refetch()} retrying={isFetching} />;
  return <PlaylistView playlist={playlist} />;
}

function PlaylistView({ playlist }: { playlist: UserPlaylistDetail }) {
  const { t, locale } = useI18n();
  const navigate = useNavigate();
  const providerLabel = useProviderLabel();
  const errorText = useErrorText();
  const server = useServerAction();
  const failure = usePlaylistFailure();
  const refresh = useRefreshPlaylists();
  const announcePending = useAnnouncePending();
  const { play } = usePlayer();
  const { data: list } = useUserPlaylists();
  const autoId = useId();
  const followed = playlist.kind === "followed";
  const tracks = playlist.tracks;
  const unavailable = playlist.tracks_unavailable;

  const refreshed = () => refresh(playlist.id);

  const autoDownload = useMutation({
    mutationFn: (value: boolean) => updateUserPlaylist(playlist.id, { auto_download: value }),
    onSuccess: refreshed,
    onError: (e) => failure(e, t("playlists.updateFailed")),
  });
  const reorder = useMutation({
    mutationFn: (ids: string[]) => reorderPlaylistTracks(playlist.id, ids),
    onSettled: refreshed,
    onError: (e) => failure(e, t("playlists.reorderFailed")),
  });
  const remove = useMutation({
    mutationFn: (libraryId: string) => removePlaylistTrack(playlist.id, libraryId),
    onSuccess: refreshed,
    onError: (e) => failure(e, t("playlists.removeFailed")),
  });
  const check = useMutation({
    mutationFn: () => refreshUserPlaylist(playlist.id),
    onSuccess: (result) => {
      refreshed();
      if (result.error_code) toast.warning(errorText(result.error_code, t("playlists.refreshFailed")));
      else if (result.changed) toast.success(t("playlists.refreshedChanges", { added: result.new_tracks, removed: result.removed_tracks }));
      else toast.info(t("playlists.upToDate"));
      announceDownload(result.download);
    },
    onError: (e) => failure(e, t("playlists.refreshFailed")),
  });

  function announceDownload(download: PlaylistDownload) {
    if (download.status === "pending_approval" && download.needed_tracks !== undefined && download.reason) {
      announcePending({ needed_tracks: download.needed_tracks, reason: download.reason });
    } else if (download.status === "queued" && download.queued) {
      toast.success(t("common.queuedTracks", { count: download.queued }));
    }
  }

  const ids = useMemo(() => tracks.map((track) => track.library_id).filter((libraryId): libraryId is string => !!libraryId), [tracks]);
  const move = (index: number, step: -1 | 1) => {
    const next = [...ids];
    const target = index + step;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    reorder.mutate(next);
  };
  const playAll = () => {
    const queue = tracks.map(toPlayerTrack);
    const first = queue.find(isPlayable);
    if (first) play(first, queue);
  };

  const mirrored = playlist.synced_to.map((copy) => providerLabel(copy.sync_provider)).join(", ");
  const maxTracks = list?.max_tracks;
  const busy = reorder.isPending || remove.isPending || server.disabled;
  const refreshProblem = playlist.refresh_error_code
    ? playlist.refresh_error_code === "playlist_source_not_found"
      ? t("playlists.refreshSourceGone")
      : t("playlists.refreshError", { error: errorText(playlist.refresh_error_code, playlist.refresh_error_code) })
    : null;

  return (
    <div className="space-y-8">
      <BackButton />
      <DetailHeader
        kind="playlist"
        cover={playlist.cover_url}
        eyebrow={followed && playlist.source_provider ? t("playlists.followedFrom", { provider: providerLabel(playlist.source_provider) }) : t("playlists.ownEyebrow")}
        title={playlist.name}
        subtitle={playlist.description ? <p className="line-clamp-3 text-sm">{playlist.description}</p> : undefined}
        meta={
          <>
            {maxTracks && !followed ? t("playlists.capacity", { count: playlist.track_count, max: maxTracks }) : t("common.trackCount", { count: playlist.track_count })}
            {" · "}
            {mirrored ? t("playlists.mirroredOn", { servers: mirrored }) : t("playlists.notMirrored")}
          </>
        }
        actions={
          <>
            {tracks.length > 0 && (
              <Button size="lg" variant="outline" className="px-5" onClick={playAll}>
                <Play aria-hidden />
                {t("playlists.playAll")}
              </Button>
            )}
            {followed && (
              <Button size="lg" variant="outline" className="px-5" onClick={() => check.mutate()} disabled={check.isPending || server.disabled} title={server.reason}>
                <RefreshCw aria-hidden className={check.isPending ? "animate-spin" : undefined} />
                {t(check.isPending ? "playlists.refreshing" : "playlists.refresh")}
              </Button>
            )}
            <UserPlaylistMenu playlist={playlist} onDeleted={() => void navigate({ to: "/playlists" })}>
              {({ button }) => button}
            </UserPlaylistMenu>
          </>
        }
      >
        {followed && (
          <div className="flex items-start gap-3 text-left">
            <Switch id={autoId} checked={playlist.auto_download} disabled={autoDownload.isPending || server.disabled} onCheckedChange={(value) => autoDownload.mutate(value)} className="mt-0.5" />
            <div className="space-y-0.5">
              <label htmlFor={autoId} className="text-sm font-medium text-foreground">
                {t("playlists.autoDownload")}
              </label>
              <p className="text-[13px]">{t("playlists.autoDownloadHint")}</p>
            </div>
          </div>
        )}
      </DetailHeader>

      {followed && <p className="text-sm text-muted-foreground">{t("playlists.readOnly")}{playlist.checked_at ? ` ${t("playlists.checkedAt", { date: absoluteTime(playlist.checked_at, locale) })}` : ""}</p>}
      {refreshProblem && (
        <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {refreshProblem}
        </div>
      )}

      {unavailable ? (
        <div role="alert" className="flex items-start gap-3 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm">
          <Lock aria-hidden className="mt-0.5 size-5 shrink-0 text-warning" />
          <div className="min-w-0 flex-1 space-y-1">
            <p className="font-medium">{t(unavailable.code === "access_restricted" ? "playlist.restrictedTitle" : "playlist.unavailableTitle")}</p>
            <p className="text-muted-foreground">{errorText(`playlist_${unavailable.code}`, unavailable.message)}</p>
          </div>
        </div>
      ) : tracks.length === 0 ? (
        <EmptyState icon={ListMusic} message={t(followed ? "playlists.emptyFollowed" : "playlists.emptyTracks")} />
      ) : (
        <section aria-label={t("playlists.tracks")}>
          <PlaylistTrackList
            tracks={tracks}
            label={playlist.name}
            editable={!followed}
            busy={busy}
            onMove={move}
            onRemove={(track) => track.library_id && remove.mutate(track.library_id)}
          />
        </section>
      )}
    </div>
  );
}
