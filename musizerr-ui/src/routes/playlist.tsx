import { useMemo, useState } from "react";
import { useLocation, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { ListMusic, Lock, Play, RotateCcw } from "lucide-react";
import { getPlaylist } from "@/lib/api-client";
import { isPlayable, toPlayerTrack, usePlayer } from "@/contexts/PlayerContext";
import { useTrackFilters } from "@/hooks/useTrackFilters";
import { useI18n } from "@/i18n";
import { BackButton } from "@/components/BackButton";
import { EmptyState } from "@/components/ContentList";
import { DetailError } from "@/components/DetailError";
import { DetailHeader, TechnicalDetails } from "@/components/DetailHeader";
import { CollectionDownloadAction, CollectionFavoriteAction } from "@/components/DownloadCollectionButton";
import { ErrorMessage } from "@/components/ErrorMessage";
import { CoverageBar } from "@/components/LibraryCoverage";
import { ListToolbar } from "@/components/ListToolbar";
import { FollowPlaylistButton } from "@/components/playlists/FollowPlaylistButton";
import { useProviderLabel } from "@/components/ProviderBadges";
import { TrackList } from "@/components/tracks/TrackList";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { ExternalId, Playlist } from "@/types";

export function PlaylistPage() {
  const { provider, id } = useParams({ from: "/playlist/$provider/$id" });
  const { t } = useI18n();
  const location = useLocation();
  const [stateItem] = useState(() => (location.state as { item?: Playlist } | null)?.item);

  const { data: playlist, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ["playlist", provider, id],
    queryFn: () => getPlaylist(provider, id) as Promise<Playlist>,
    placeholderData: stateItem ?? undefined,
    select: (data) => {
      if (!stateItem) return data;
      const seen = new Set(data.external_ids.map((e: ExternalId) => `${e.type}:${e.value}`));
      return {
        ...data,
        cover_url: data.cover_url ?? stateItem.cover_url,
        external_ids: [...data.external_ids, ...stateItem.external_ids.filter((e: ExternalId) => !seen.has(`${e.type}:${e.value}`))],
      };
    },
  });

  if (isLoading) return <Skeleton className="h-56 rounded-lg" />;
  if (!playlist) return <DetailError error={error} icon={ListMusic} notFound={t("playlist.notFound")} onRetry={() => void refetch()} retrying={isFetching} />;
  return <PlaylistView playlist={playlist} provider={provider} id={id} onRetry={() => void refetch()} retrying={isFetching} />;
}

function PlaylistView({
  playlist,
  provider,
  id,
  onRetry,
  retrying,
}: {
  playlist: Playlist;
  provider: string;
  id: string;
  onRetry: () => void;
  retrying: boolean;
}) {
  const { t } = useI18n();
  const providerLabel = useProviderLabel();
  const { play } = usePlayer();
  const filters = useTrackFilters(playlist.tracks);
  const description = useMemo(() => plainText(playlist.description), [playlist.description]);
  const unavailable = playlist.tracks_unavailable;
  const count = playlist.track_count ?? playlist.tracks.length;

  const playAll = () => {
    const queue = playlist.tracks.map(toPlayerTrack);
    const first = queue.find(isPlayable);
    if (first) play(first, queue);
  };

  return (
    <div className="space-y-8">
      <BackButton />
      <DetailHeader
        kind="playlist"
        cover={playlist.cover_url}
        eyebrow={`${t("tile.playlist")} · ${providerLabel(playlist.provider)}`}
        title={playlist.name}
        subtitle={description ? <p className="line-clamp-3 text-sm">{description}</p> : undefined}
        meta={t("common.trackCount", { count })}
        actions={
          <>
            {playlist.tracks.length > 0 && (
              <Button size="lg" variant="outline" className="px-5" onClick={playAll}>
                <Play aria-hidden />
                {t("album.play")}
              </Button>
            )}
            <CollectionDownloadAction
              kind="playlist"
              provider={provider}
              id={id}
              name={playlist.name}
              library={playlist.library}
              unavailableReason={unavailable ? t("playlist.cannotDownload") : playlist.tracks.length === 0 ? t("album.empty") : undefined}
            />
            {!unavailable && <CollectionFavoriteAction tracks={playlist.tracks} favorites={playlist.favorites} onChanged={onRetry} />}
            <FollowPlaylistButton provider={provider} id={id} name={playlist.name} tracks={playlist.track_count ?? playlist.tracks.length} />
          </>
        }
      >
        {playlist.library && !unavailable && <CoverageBar coverage={playlist.library} />}
      </DetailHeader>

      {unavailable ? (
        <div role="alert" className="flex flex-col items-start gap-3 rounded-lg border border-warning/30 bg-warning/10 p-4 text-sm sm:flex-row sm:items-center">
          <Lock aria-hidden className="size-5 shrink-0 text-warning" />
          <div className="min-w-0 flex-1 space-y-1">
            <p className="font-medium">{t(unavailable.code === "access_restricted" ? "playlist.restrictedTitle" : "playlist.unavailableTitle")}</p>
            <ErrorMessage className="text-muted-foreground" code={`playlist_${unavailable.code}`} message={unavailable.message} />
          </div>
          <Button variant="outline" onClick={onRetry} disabled={retrying}>
            <RotateCcw aria-hidden className={retrying ? "animate-spin" : undefined} />
            {t("common.retry")}
          </Button>
        </div>
      ) : playlist.tracks.length === 0 ? (
        <EmptyState icon={ListMusic} message={t("album.empty")} />
      ) : (
        <section aria-label={t("album.tracks")} className="space-y-3">
          {playlist.tracks.length > 1 && (
            <ListToolbar filter={{ value: filters.query, onChange: filters.setQuery, placeholder: t("album.filterPlaceholder") }} chips={filters.chips} />
          )}
          {filters.filtered.length === 0 ? (
            <EmptyState icon={ListMusic} message={t("album.noMatch")} />
          ) : (
            <TrackList tracks={filters.filtered} numbering="index" header label={playlist.name} />
          )}
        </section>
      )}

      <TechnicalDetails providers={[playlist.provider]} externalIds={playlist.external_ids} />
    </div>
  );
}

/**
 * Some providers put markup and entities in playlist descriptions: keep the
 * text only. DOMParser builds an inert document (nothing runs or loads) and
 * the result is rendered as text, never as HTML.
 */
export function plainText(html: string | null | undefined): string {
  if (!html) return "";
  if (!/[<&]/.test(html)) return html;
  return (new DOMParser().parseFromString(html, "text/html").body.textContent ?? "").trim();
}
