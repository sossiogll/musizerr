import { useState } from "react";
import { Link, useLocation, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Clock, Disc3, Download, Hourglass, Loader2, MicVocal, Music, Pause, Play, RotateCcw } from "lucide-react";
import { getAppearsOn, getRelatedTracks, getTrack } from "@/lib/api-client";
import { mergeExternalIds } from "@/lib/enrichment";
import { formatDuration } from "@/lib/utils";
import { isPlayable, toPlayerTrack, usePlayer } from "@/contexts/PlayerContext";
import { useDownloadStates } from "@/hooks/useDownloadStates";
import { useEnrichedFetch } from "@/hooks/useEnrichedFetch";
import { useTrackDownload, type TrackDownload } from "@/hooks/useTrackDownload";
import { useTrackFavorite, type HeartState } from "@/hooks/useTrackFavorite";
import { DownloadButton } from "@/components/DownloadButton";
import { TrackMenu } from "@/components/menu/TrackMenu";
import { LyricsBody, useTrackLyrics } from "@/components/lyrics/Lyrics";
import { FavoriteHeart } from "@/components/tracks/FavoriteHeart";
import { useI18n } from "@/i18n";
import { ArtistLinks } from "@/components/ArtistLinks";
import { BackButton } from "@/components/BackButton";
import { DetailError } from "@/components/DetailError";
import { DetailHeader, TechnicalDetails } from "@/components/DetailHeader";
import { IncompleteResults } from "@/components/IncompleteResults";
import { useProviderLabel } from "@/components/ProviderBadges";
import { TrackList } from "@/components/tracks/TrackList";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { Section } from "./discover";
import type { AlbumRef, Lyrics, Track } from "@/types";

export function TrackPage() {
  const { provider, id } = useParams({ from: "/track/$provider/$id" });
  const { t } = useI18n();
  const location = useLocation();
  const [stateItem] = useState(() => (location.state as { item?: Track } | null)?.item);

  const { data: track, isLoading, error, refetch, isFetching } = useEnrichedFetch({
    queryKey: ["track", provider, id],
    provider,
    id,
    stateItem,
    fetchFn: getTrack,
    mergeWithState: (fetched, state) => {
      const seenAlbumNames = new Set((fetched.albums ?? []).map((a: AlbumRef) => a.name));
      return {
        ...fetched,
        cover_url: fetched.cover_url ?? state.cover_url,
        external_ids: mergeExternalIds(fetched.external_ids, state.external_ids),
        albums: [...(fetched.albums ?? []), ...(state.albums ?? []).filter((a: AlbumRef) => !seenAlbumNames.has(a.name))],
      };
    },
  });

  if (isLoading) return <TrackSkeleton />;
  if (!track) return <DetailError error={error} icon={Music} notFound={t("track.notFound")} onRetry={() => void refetch()} retrying={isFetching} />;
  return <TrackView track={track} provider={provider} id={id} />;
}

function TrackView({ track, provider, id }: { track: Track; provider: string; id: string }) {
  const { t } = useI18n();
  const lyrics = useTrackLyrics(provider, id);
  const appearsOn = useQuery({ queryKey: ["track-appears-on", provider, id], queryFn: () => getAppearsOn(provider, id) });
  const related = useQuery({ queryKey: ["track-related", provider, id], queryFn: () => getRelatedTracks(provider, id) });

  const albums: AlbumRef[] = track.albums.length
    ? track.albums
    : track.album
      ? [{ name: track.album, provider: track.provider, provider_id: track.album_provider_id }]
      : [];

  return (
    <div className="space-y-8">
      <BackButton />
      <DetailHeader
        kind="track"
        cover={track.cover_url}
        eyebrow={[t("track.eyebrow"), track.track_number ? t("track.number", { number: track.track_number }) : null, track.year].filter(Boolean).join(" · ")}
        title={track.title}
        subtitle={<ArtistLinks artists={track.artists} fallback={track.artist} />}
        meta={
          <span className="inline-flex flex-wrap items-center justify-center gap-x-2 sm:justify-start">
            {albums.map((a, i) => (
              <span key={`${a.provider}:${a.provider_id || a.name}`} className="inline-flex items-center gap-1">
                {i === 0 && <Disc3 aria-hidden className="size-3.5" />}
                {a.provider_id ? (
                  <Link to="/album/$provider/$id" params={{ provider: a.provider, id: a.provider_id }} className="hover:underline">
                    {a.name}
                  </Link>
                ) : (
                  a.name
                )}
                {i < albums.length - 1 && ","}
              </span>
            ))}
            {track.duration_ms > 0 && (
              <span className="inline-flex items-center gap-1 tabular-nums">
                <Clock aria-hidden className="size-3.5" />
                {formatDuration(track.duration_ms)}
              </span>
            )}
          </span>
        }
        actions={<TrackActions track={track} />}
      />

      <div className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        {/* From L: as tall as the right column (20rem at least), without adding the lyrics' own height to the row. */}
        <div className="min-w-0 lg:min-h-80 lg:[contain:size]">
          <LyricsPanel lyrics={lyrics.data} loading={lyrics.isLoading} track={track} />
        </div>
        <div className="min-w-0 space-y-8">
          {(!!appearsOn.data?.items.length || !!appearsOn.data?.provider_errors.length) && (
            <Section title={t("track.appearsOn")}>
              {!!appearsOn.data.items.length && (
              <ul className="divide-y rounded-lg border">
                {appearsOn.data.items.map((release) => (
                  <li key={`${release.provider}:${release.provider_id}`}>
                    <Link
                      to="/album/$provider/$id"
                      params={{ provider: release.provider, id: release.provider_id }}
                      className="flex min-h-12 items-center justify-between gap-3 px-4 py-2 text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus-visible:outline-none"
                    >
                      <span className="truncate font-medium">{release.title}</span>
                      {release.year && <span className="shrink-0 tabular-nums text-muted-foreground">{release.year}</span>}
                    </Link>
                  </li>
                ))}
              </ul>
              )}
              <IncompleteResults variant="inline" errors={appearsOn.data.provider_errors} />
            </Section>
          )}
          {(!!related.data?.items.length || !!related.data?.provider_errors.length) && (
            <Section title={t("track.related")}>
              {!!related.data.items.length && <TrackList tracks={related.data.items} showAlbum={false} label={t("track.related")} />}
              <IncompleteResults variant="inline" errors={related.data.provider_errors} />
            </Section>
          )}
          <TechnicalDetails providers={track.providers} externalIds={track.external_ids} />
        </div>
      </div>
    </div>
  );
}

/** Play/pause, the track's file state and heart, and its menu (queue, playlists, go to…), as the page's actions. */
function TrackActions({ track }: { track: Track }) {
  const { t } = useI18n();
  const { track: current, playing, play } = usePlayer();
  const downloadState = useTrackDownload(track);
  const heart = useTrackFavorite(track, downloadState.slot, downloadState.libraryId, downloadState.favoriteQueued);
  const playable = isPlayable(track);
  const isCurrent = current?.provider === track.provider && current?.provider_id === track.provider_id;
  const isPlaying = isCurrent && playing;

  return (
    <>
      {playable && (
        <Button size="lg" className="px-5" onClick={() => play(toPlayerTrack(track))}>
          {isPlaying ? <Pause aria-hidden /> : <Play aria-hidden />}
          {isPlaying ? t("player.pause") : t("album.play")}
        </Button>
      )}
      <TrackDownloadAction track={track} download={downloadState} heart={heart} />
      <TrackMenu track={track} download={downloadState} heart={heart}>
        {({ button }) => button}
      </TrackMenu>
    </>
  );
}

/** The track's file state as the page's action: Download, Retry, or where it stands — and the heart. */
function TrackDownloadAction({ track, download: state, heart }: { track: Track; download: TrackDownload; heart: HeartState }) {
  const { t } = useI18n();
  const { label } = useDownloadStates();
  const { slot, modes, download, busy, disabledReason, approval } = state;
  const heartNode = heart.kind === "hidden" ? null : <FavoriteHeart state={heart} title={track.title} />;
  if (slot.kind === "none") return heartNode;
  if (slot.kind === "available") {
    return (
      <>
        <DownloadButton
          modes={modes}
          onDownload={download}
          icon={busy ? <Loader2 aria-hidden className="animate-spin" /> : <Download aria-hidden />}
          text={t(approval.waits ? "approval.request" : "track.download")}
          labels={{ plain: t("slot.download"), favorite: approval.waits ? t("approval.requestItem", { title: track.title }) : t("slot.downloadWithFavorite") }}
          menuLabel={t("slot.downloadOptions", { title: track.title })}
          disabled={busy || !!disabledReason}
          title={disabledReason ?? approval.hint}
          className={buttonVariants({ variant: "brand", size: "lg", className: "px-5" })}
          menuClassName={buttonVariants({ variant: "brand", size: "lg", className: "rounded-l-none border-l border-white/30 px-3" })}
        />
        {heartNode}
      </>
    );
  }
  if (slot.kind === "awaiting") {
    return (
      <>
        <Badge variant="warning" role="status" className="h-11 gap-1.5 rounded-md px-4 text-sm">
          <Hourglass aria-hidden className="size-4" />
          {t("approval.waiting")}
        </Badge>
        {heartNode}
      </>
    );
  }
  if (slot.phase === "failed") {
    return (
      <>
        <Button
          size="lg"
          variant="outline"
          className="px-5"
          onClick={() => download(modes[modes.length - 1] ?? "favorite")}
          disabled={busy || !!disabledReason}
          title={disabledReason}
        >
          {busy ? <Loader2 aria-hidden className="animate-spin" /> : <RotateCcw aria-hidden />}
          {t("slot.retry")}
        </Button>
        {heartNode}
      </>
    );
  }
  const Icon = slot.phase === "done" ? CheckCircle2 : slot.phase === "active" ? Loader2 : Clock;
  return (
    <>
      <Badge variant="secondary" role="status" className="h-11 gap-1.5 rounded-md px-4 text-sm">
        <Icon aria-hidden className={cn("size-4", slot.phase === "done" && "text-success", slot.phase === "active" && "animate-spin text-primary")} />
        {label(slot.status)}
      </Badge>
      {heartNode}
    </>
  );
}

/**
 * The lyrics. Below L they are part of the page. From L they fill the column
 * up to the screen height left between the shell's top bar and player, stick
 * under the top bar while the right column scrolls by, and scroll on their
 * own. Synced lyrics follow the player while this track plays in full.
 */
function LyricsPanel({ lyrics, loading, track }: { lyrics: Lyrics | undefined; loading: boolean; track: Track }) {
  const { t } = useI18n();
  const providerLabel = useProviderLabel();
  const { track: current, playbackType } = usePlayer();
  const following = current?.provider === track.provider && current?.provider_id === track.provider_id && playbackType === "full";

  return (
    <section
      aria-labelledby="lyrics"
      className="min-w-0 space-y-3 lg:sticky lg:top-[calc(var(--shell-top)_+_1rem)] lg:flex lg:h-[min(100%,calc(100dvh_-_var(--shell-top)_-_var(--player-h)_-_2rem))] lg:flex-col"
    >
      <div className="flex items-center gap-2">
        <MicVocal aria-hidden className="size-5 text-muted-foreground" />
        <h2 id="lyrics" className="font-display text-lg font-semibold">
          {t("track.lyrics")}
        </h2>
        {following && !!lyrics?.synced.length && <Badge className="ml-1">{t("track.synced")}</Badge>}
        {lyrics?.source && <span className="ml-auto text-xs text-muted-foreground">{t("common.via", { provider: providerLabel(lyrics.source) })}</span>}
      </div>
      <LyricsBody
        lyrics={lyrics}
        loading={loading}
        following={following}
        labelledBy="lyrics"
        boxClassName="lg:min-h-0 lg:flex-1 lg:overflow-y-auto lg:overscroll-contain"
      />
    </section>
  );
}

function TrackSkeleton() {
  return (
    <div aria-hidden className="flex flex-col items-center gap-5 sm:flex-row sm:items-end">
      <Skeleton className="size-44 rounded-xl lg:size-56" />
      <div className="w-full flex-1 space-y-3">
        <Skeleton className="mx-auto h-4 w-24 sm:mx-0" />
        <Skeleton className="mx-auto h-10 w-2/3 sm:mx-0" />
        <Skeleton className="mx-auto h-4 w-40 sm:mx-0" />
      </div>
    </div>
  );
}
