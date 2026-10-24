import { useMemo, useState } from "react";
import { useLocation, useNavigate, useParams } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { Disc3, Play } from "lucide-react";
import { getAlbum, getAlbumEditions } from "@/lib/api-client";
import { mergeExternalIds } from "@/lib/enrichment";
import { isPlayable, toPlayerTrack, usePlayer } from "@/contexts/PlayerContext";
import { useEnrichedFetch } from "@/hooks/useEnrichedFetch";
import { useTrackFilters } from "@/hooks/useTrackFilters";
import { useI18n, type MessageKey } from "@/i18n";
import { ArtistLinks } from "@/components/ArtistLinks";
import { BackButton } from "@/components/BackButton";
import { EmptyState } from "@/components/ContentList";
import { DetailError } from "@/components/DetailError";
import { DetailHeader, TechnicalDetails } from "@/components/DetailHeader";
import { CollectionDownloadAction, CollectionFavoriteAction } from "@/components/DownloadCollectionButton";
import { CoverageBar } from "@/components/LibraryCoverage";
import { ListToolbar, type SortOption } from "@/components/ListToolbar";
import { TrackList } from "@/components/tracks/TrackList";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { Album, AlbumEdition, Track } from "@/types";

type AlbumTrackSort = "number" | "title";

const SORT_OPTIONS: { value: AlbumTrackSort; labelKey: MessageKey }[] = [
  { value: "number", labelKey: "album.sortTrackNumber" },
  { value: "title", labelKey: "home.sortTitle" },
];

export function AlbumPage() {
  const { provider, id } = useParams({ from: "/album/$provider/$id" });
  const { t } = useI18n();
  const location = useLocation();
  const [stateItem] = useState(() => (location.state as { item?: Album } | null)?.item);

  const { data: album, isLoading, error, refetch, isFetching } = useEnrichedFetch({
    queryKey: ["album", provider, id],
    provider,
    id,
    stateItem,
    fetchFn: getAlbum,
    mergeWithState: (fetched, state) => ({
      ...fetched,
      cover_url: fetched.cover_url ?? state.cover_url,
      year: fetched.year ?? state.year,
      external_ids: mergeExternalIds(fetched.external_ids, state.external_ids),
    }),
  });

  if (isLoading) return <AlbumSkeleton />;
  if (!album) return <DetailError error={error} icon={Disc3} notFound={t("album.notFound")} onRetry={() => void refetch()} retrying={isFetching} />;
  return <AlbumView album={album} provider={provider} id={id} onChanged={() => void refetch()} />;
}

function AlbumView({ album, provider, id, onChanged }: { album: Album; provider: string; id: string; onChanged: () => void }) {
  const { t, tx } = useI18n();
  const { play } = usePlayer();
  const [sort, setSort] = useState<AlbumTrackSort>("number");
  const filters = useTrackFilters(album.tracks);
  const sortOptions: SortOption<AlbumTrackSort>[] = useMemo(() => SORT_OPTIONS.map((o) => ({ value: o.value, label: t(o.labelKey) })), [t]);
  const tracks = useMemo(
    () => (sort === "title" ? [...filters.filtered].sort((a, b) => a.title.localeCompare(b.title)) : filters.filtered),
    [filters.filtered, sort],
  );
  const discs = useMemo(() => (sort === "number" ? byDisc(tracks) : [{ disc: null, tracks }]), [tracks, sort]);

  const durationMs = album.tracks.reduce((sum, track) => sum + (track.duration_ms || 0), 0);
  const trackCount = album.track_count ?? album.tracks.length;
  const playAll = () => {
    const queue = album.tracks.map(toPlayerTrack);
    const first = queue.find(isPlayable);
    if (first) play(first, queue);
  };

  return (
    <div className="space-y-8">
      <BackButton />
      <DetailHeader
        kind="album"
        cover={album.cover_url}
        eyebrow={[album.album_type ? tx(`albumType.${album.album_type}`, album.album_type) : t("albumType.album"), album.year].filter(Boolean).join(" · ")}
        title={album.title}
        subtitle={<ArtistLinks artists={album.artists} fallback={album.artist} />}
        meta={[t("common.trackCount", { count: trackCount }), durationMs > 0 && t("album.minutes", { count: Math.round(durationMs / 60000) })]
          .filter(Boolean)
          .join(" · ")}
        actions={
          <>
            <Button size="lg" variant="outline" className="px-5" onClick={playAll} disabled={!album.tracks.some(isPlayable)}>
              <Play aria-hidden />
              {t("album.play")}
            </Button>
            <CollectionDownloadAction
              kind="album"
              provider={provider}
              id={id}
              name={album.title}
              library={album.library}
              unavailableReason={album.tracks.length === 0 ? t("album.empty") : undefined}
            />
            <CollectionFavoriteAction tracks={album.tracks} favorites={album.favorites} onChanged={onChanged} />
          </>
        }
      >
        <div className="space-y-3">
          {album.library && <CoverageBar coverage={album.library} />}
          <EditionPicker provider={provider} id={id} />
        </div>
      </DetailHeader>

      {album.enrichment && !album.enrichment.complete && (
        <p role="status" className="text-sm text-muted-foreground">
          {t("album.enrichmentIncomplete")}
        </p>
      )}

      {album.tracks.length > 0 && (
        <section aria-labelledby="album-tracks" className="space-y-3">
          <h2 id="album-tracks" className="sr-only">
            {t("album.tracks")}
          </h2>
          {album.tracks.length > 1 && (
            <ListToolbar<AlbumTrackSort>
              filter={{ value: filters.query, onChange: filters.setQuery, placeholder: t("album.filterPlaceholder") }}
              chips={filters.chips}
              sort={{ value: sort, onChange: setSort, options: sortOptions }}
            />
          )}
          {tracks.length === 0 ? (
            <EmptyState icon={Disc3} message={filters.active ? t("album.noMatch") : t("album.empty")} />
          ) : (
            discs.map(({ disc, tracks: discTracks }) => (
              <div key={disc ?? "all"} className="space-y-1">
                {disc !== null && (
                  <h3 className="flex items-center gap-2 px-2 pt-2 text-sm font-semibold text-muted-foreground">
                    <Disc3 aria-hidden className="size-4" />
                    {t("album.disc", { number: disc })}
                  </h3>
                )}
                <TrackList
                  tracks={discTracks}
                  numbering={sort === "number" ? "track" : "index"}
                  showCover={false}
                  showAlbum={false}
                  albumInSubtitle={false}
                  header={disc === null || disc === discs[0].disc}
                  label={disc !== null ? t("album.disc", { number: disc }) : t("album.tracks")}
                />
              </div>
            ))
          )}
        </section>
      )}

      <TechnicalDetails providers={album.providers} externalIds={album.external_ids} />
    </div>
  );
}

/** Tracks split by disc when the album has more than one, in track order. */
function byDisc(tracks: Track[]): { disc: number | null; tracks: Track[] }[] {
  const numbers = new Set(tracks.map((track) => track.disc_number ?? 1));
  if (numbers.size <= 1) return [{ disc: null, tracks }];
  return [...numbers]
    .sort((a, b) => a - b)
    .map((disc) => ({ disc, tracks: tracks.filter((track) => (track.disc_number ?? 1) === disc) }));
}

/** Other editions of the same release (deluxe, remaster...), when there are any. */
function EditionPicker({ provider, id }: { provider: string; id: string }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { data } = useQuery({
    queryKey: ["album-editions", provider, id],
    queryFn: () => getAlbumEditions(provider, id),
    staleTime: 10 * 60 * 1000,
  });
  const editions = data?.items ?? [];
  if (editions.length < 2) return null;
  const label = (e: AlbumEdition) =>
    [e.title, e.year, e.track_count ? t("common.trackCount", { count: e.track_count }) : null].filter(Boolean).join(" · ");
  return (
    <label className="flex flex-col gap-1 text-left text-xs text-muted-foreground sm:flex-row sm:items-center sm:gap-2">
      <span className="shrink-0">{t("album.edition", { count: editions.length })}</span>
      <select
        value={`${provider}\u0000${id}`}
        onChange={(e) => {
          const [p, i] = e.target.value.split("\u0000");
          void navigate({ to: "/album/$provider/$id", params: { provider: p, id: i } });
        }}
        className="h-10 w-full min-w-0 rounded-md border bg-background px-3 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:max-w-xs"
      >
        {editions.map((e) => (
          <option key={`${e.provider}:${e.provider_id}`} value={`${e.provider}\u0000${e.provider_id}`}>
            {label(e)}
          </option>
        ))}
      </select>
    </label>
  );
}

function AlbumSkeleton() {
  return (
    <div aria-hidden className="space-y-8">
      <div className="flex flex-col items-center gap-5 sm:flex-row sm:items-end">
        <Skeleton className="size-44 rounded-xl lg:size-56" />
        <div className="w-full flex-1 space-y-3">
          <Skeleton className="mx-auto h-4 w-24 sm:mx-0" />
          <Skeleton className="mx-auto h-10 w-2/3 sm:mx-0" />
          <Skeleton className="mx-auto h-4 w-40 sm:mx-0" />
        </div>
      </div>
      <div className="space-y-1">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-14" />
        ))}
      </div>
    </div>
  );
}
