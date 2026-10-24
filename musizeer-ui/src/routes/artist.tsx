import { useMemo, useState } from "react";
import { useLocation, useParams } from "@tanstack/react-router";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { Disc3 } from "lucide-react";
import { getArtist, getArtistAlbums, getArtistTopTracks } from "@/lib/api-client";
import { mergeExternalIds } from "@/lib/enrichment";
import { useEnrichedFetch } from "@/hooks/useEnrichedFetch";
import { useI18n, type MessageKey } from "@/i18n";
import { BackButton } from "@/components/BackButton";
import { EmptyState, LoadMoreButton } from "@/components/ContentList";
import { DetailError } from "@/components/DetailError";
import { DetailHeader, TechnicalDetails } from "@/components/DetailHeader";
import { DownloadCollectionButton } from "@/components/DownloadCollectionButton";
import { ApiErrorMessage, ErrorMessage } from "@/components/ErrorMessage";
import { IncompleteResults } from "@/components/IncompleteResults";
import { CoverageBar, CoverageShort } from "@/components/LibraryCoverage";
import { ListToolbar, type SortOption } from "@/components/ListToolbar";
import { MediaTile, TILE_GRID } from "@/components/MediaTile";
import { TrackList } from "@/components/tracks/TrackList";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Section } from "./discover";
import type { Album, AlbumType, Artist, ProviderError, TopTracksResponse } from "@/types";

type DiscographySort = "year_desc" | "year_asc" | "title";

const SORT_OPTIONS: { value: DiscographySort; labelKey: MessageKey }[] = [
  { value: "year_desc", labelKey: "home.sortYearNewest" },
  { value: "year_asc", labelKey: "home.sortYearOldest" },
  { value: "title", labelKey: "home.sortTitle" },
];

// The server's order of release types (core/discography.ALBUM_TYPES); a
// release without a type goes last.
const TYPE_SECTIONS: { type: AlbumType | null; labelKey: MessageKey }[] = [
  { type: "album", labelKey: "artist.typeAlbum" },
  { type: "single", labelKey: "artist.typeSingle" },
  { type: "ep", labelKey: "artist.typeEp" },
  { type: "compilation", labelKey: "artist.typeCompilation" },
  { type: "live", labelKey: "artist.typeLive" },
  { type: "appears_on", labelKey: "artist.typeAppearsOn" },
  { type: null, labelKey: "artist.typeOther" },
];

const TOP_SHOWN = 5;

function filterAndSort(items: Album[], filter: string, sort: DiscographySort): Album[] {
  const q = filter.trim().toLowerCase();
  const result = q ? items.filter((a) => a.title.toLowerCase().includes(q)) : [...items];
  result.sort((a, b) => {
    switch (sort) {
      case "year_desc":
        return (b.year ?? 0) - (a.year ?? 0);
      case "year_asc":
        return (a.year ?? 9999) - (b.year ?? 9999);
      case "title":
        return a.title.localeCompare(b.title);
    }
  });
  return result;
}

export function ArtistPage() {
  const { provider, id } = useParams({ from: "/artist/$provider/$id" });
  const { t } = useI18n();
  const location = useLocation();
  const [stateItem] = useState(() => (location.state as { item?: Artist } | null)?.item);

  const { data: artist, isLoading, error, refetch, isFetching } = useEnrichedFetch({
    queryKey: ["artist", provider, id],
    provider,
    id,
    stateItem,
    fetchFn: getArtist,
    mergeWithState: (fetched, state) => ({
      ...fetched,
      cover_url: fetched.cover_url ?? state.cover_url,
      external_ids: mergeExternalIds(fetched.external_ids, state.external_ids),
    }),
  });
  const albums = useQuery({
    queryKey: ["artist-albums", provider, id],
    queryFn: () => getArtistAlbums(provider, id),
    enabled: !!artist,
  });
  const top = useQuery({
    queryKey: ["artist-top-tracks", provider, id],
    queryFn: () => getArtistTopTracks(provider, id),
    enabled: !!artist,
  });

  if (isLoading) return <HeaderSkeleton />;
  if (!artist) return <DetailError error={error} icon={Disc3} notFound={t("artist.notFound")} onRetry={() => void refetch()} retrying={isFetching} />;

  const releases = albums.data?.total;
  return (
    <div className="space-y-8">
      <BackButton />
      <DetailHeader
        kind="artist"
        cover={artist.cover_url}
        eyebrow={t("tile.artist")}
        title={artist.name}
        meta={releases !== undefined ? t("artist.releaseCount", { count: releases }) : undefined}
        actions={
          // Only where the provider can give the whole discography (the server says so).
          artist.discography_downloadable ? (
            <DownloadCollectionButton kind="artist" provider={provider} id={id} name={artist.name} label={t("tile.confirmArtistAction")} favoriteLabel={t("tile.downloadArtistFavorite", { title: artist.name })} variant="outline" />
          ) : undefined
        }
      >
        {albums.data?.library && <CoverageBar coverage={albums.data.library} />}
      </DetailHeader>

      <TopTracks query={top} />

      <Discography albums={albums.data?.items} loading={albums.isLoading} error={albums.error} providerErrors={albums.data?.provider_errors} mayBeLonger={albums.data?.may_be_longer} />

      <TechnicalDetails providers={artist.providers} externalIds={artist.external_ids} />
    </div>
  );
}

function TopTracks({ query }: { query: UseQueryResult<TopTracksResponse> }) {
  const { t } = useI18n();
  const [shown, setShown] = useState(TOP_SHOWN);
  const { data, isLoading } = query;
  if (isLoading) {
    return (
      <div aria-hidden className="space-y-1">
        {Array.from({ length: TOP_SHOWN }).map((_, i) => (
          <Skeleton key={i} className="h-14" />
        ))}
      </div>
    );
  }
  if (!data) return null;
  if (!data.items.length) {
    // Every provider failed: say why; nothing to show otherwise.
    return data.error_code ? (
      <Section title={t("artist.topTracks")}>
        <ErrorMessage className="text-sm text-muted-foreground" code={data.error_code} message={data.error_message} />
      </Section>
    ) : null;
  }
  return (
    <Section title={t("artist.topTracks")}>
      <TrackList tracks={data.items.slice(0, shown)} numbering="index" label={t("artist.topTracks")} />
      <LoadMoreButton visible={shown} total={data.items.length} onLoadMore={() => setShown(data.items.length)} />
      <IncompleteResults variant="inline" errors={data.provider_errors} />
    </Section>
  );
}

/** One grid per release type, in the server's order, filtered and sorted together. */
function Discography({
  albums,
  loading,
  error,
  providerErrors,
  mayBeLonger,
}: {
  albums: Album[] | undefined;
  loading: boolean;
  error: unknown;
  providerErrors: ProviderError[] | undefined;
  /** The provider's listing reached the server's bound: the artist may have more releases. */
  mayBeLonger: boolean | undefined;
}) {
  const { t } = useI18n();
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState<DiscographySort>("year_desc");
  const sortOptions: SortOption<DiscographySort>[] = useMemo(() => SORT_OPTIONS.map((o) => ({ value: o.value, label: t(o.labelKey) })), [t]);
  const sections = useMemo(() => {
    const visible = filterAndSort(albums ?? [], filter, sort);
    const known = new Set(TYPE_SECTIONS.map((s) => s.type));
    return TYPE_SECTIONS.map((section) => ({
      ...section,
      items: visible.filter((a) => (section.type === null ? !known.has(a.album_type) || a.album_type === null : a.album_type === section.type)),
    })).filter((section) => section.items.length > 0);
  }, [albums, filter, sort]);

  return (
    <section aria-labelledby="discography" className="space-y-4">
      <h2 id="discography" className="font-display text-xl font-semibold">
        {t("artist.discography")}
      </h2>
      <IncompleteResults variant="inline" errors={providerErrors} />
      {(albums?.length ?? 0) > 1 && (
        <ListToolbar<DiscographySort>
          filter={{ value: filter, onChange: setFilter, placeholder: t("artist.filterPlaceholder") }}
          sort={{ value: sort, onChange: setSort, options: sortOptions }}
        />
      )}
      {loading ? (
        <div aria-hidden className={TILE_GRID}>
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="aspect-square rounded-lg" />
          ))}
        </div>
      ) : error ? (
        <ApiErrorMessage className="text-sm text-destructive" error={error} fallback={t("artist.discographyFailed")} />
      ) : sections.length === 0 ? (
        <EmptyState icon={Disc3} message={filter ? t("artist.noMatch") : t("artist.empty")} />
      ) : (
        sections.map((section) => (
          <div key={section.labelKey} className="space-y-3">
            <h3 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
              {t(section.labelKey)} <span className="tabular-nums">· {section.items.length}</span>
            </h3>
            <ul className={TILE_GRID}>
              {section.items.map((album) => (
                <li key={`${album.provider}:${album.provider_id}`} className="min-w-0">
                  <ReleaseTile album={album} />
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
      {mayBeLonger && <p className="text-sm text-muted-foreground">{t("artist.mayBeLonger")}</p>}
    </section>
  );
}

function ReleaseTile({ album }: { album: Album }) {
  const { t } = useI18n();
  const editions = album.editions?.length ?? 0;
  return (
    <MediaTile
      item={album}
      downloadable
      badge={editions > 1 ? <Badge className="shadow-sm">{t("artist.editions", { count: editions })}</Badge> : undefined}
      meta={<CoverageShort coverage={album.library} />}
    />
  );
}

function HeaderSkeleton() {
  return (
    <div aria-hidden className="flex flex-col items-center gap-5 sm:flex-row sm:items-end">
      <Skeleton className="size-44 rounded-full lg:size-52" />
      <div className="w-full flex-1 space-y-3">
        <Skeleton className="mx-auto h-4 w-24 sm:mx-0" />
        <Skeleton className="mx-auto h-10 w-2/3 sm:mx-0" />
        <Skeleton className="mx-auto h-4 w-40 sm:mx-0" />
      </div>
    </div>
  );
}
