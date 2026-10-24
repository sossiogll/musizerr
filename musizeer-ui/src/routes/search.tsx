import { useState, useEffect, useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearch, useNavigate } from "@tanstack/react-router";
import { useDebounce } from "use-debounce";
import { Search, X, Compass } from "lucide-react";
import { search } from "@/lib/api-client";
import { usePublicProviders } from "@/hooks/usePublicProviders";
import { useCan } from "@/hooks/useCapabilities";
import { SignInToSearch } from "@/components/SignInToSearch";
import { IncompleteResults } from "@/components/IncompleteResults";
import { useI18n, type MessageKey } from "@/i18n";
import { MediaTile, TILE_GRID } from "@/components/MediaTile";
import { TrackList } from "@/components/tracks/TrackList";
import { DiscoverContent } from "./discover";
import { ContentList, LoadMoreButton } from "@/components/ContentList";
import { ListToolbar, type FilterChip, type SortOption } from "@/components/ListToolbar";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import type { Album, Artist, Playlist, SearchResult, SearchType, Track } from "@/types";

const PAGE_SIZE = 10;
const SEARCH_STALE_MS = 5 * 60 * 1000;

const TYPE_OPTIONS: { value: string; labelKey: MessageKey }[] = [
  { value: "track", labelKey: "home.typeTracks" },
  { value: "album", labelKey: "home.typeAlbums" },
  { value: "artist", labelKey: "home.typeArtists" },
  { value: "playlist", labelKey: "home.typePlaylists" },
];

type SearchSort = "relevance" | "title" | "year_desc" | "year_asc" | "providers";

const SORT_OPTIONS: { value: SearchSort; labelKey: MessageKey }[] = [
  { value: "relevance", labelKey: "home.sortRelevance" },
  { value: "title", labelKey: "home.sortTitle" },
  { value: "year_desc", labelKey: "home.sortYearNewest" },
  { value: "year_asc", labelKey: "home.sortYearOldest" },
  { value: "providers", labelKey: "home.sortProviders" },
];

// Every item carries its `kind`: rendering and sorting switch on it, never
// on which fields happen to be present.
function getTitle(item: SearchResult): string {
  return item.kind === "track" || item.kind === "album" ? item.title : item.name;
}

function getYear(item: SearchResult): number | null {
  return item.kind === "track" || item.kind === "album" ? item.year : null;
}

/** Tracks as rows (one play queue), albums, artists and playlists as a cover grid. */
function SearchResults({ items }: { items: SearchResult[] }) {
  const tracks = useMemo(() => items.filter((i): i is Track => i.kind === "track"), [items]);
  const tiles = useMemo(() => items.filter((i): i is Album | Artist | Playlist => i.kind !== "track"), [items]);
  return (
    <>
      {tracks.length > 0 && <TrackList tracks={tracks} header />}
      {tiles.length > 0 && (
        <div className={TILE_GRID}>
          {tiles.map((item) => (
            <MediaTile key={`${item.kind}:${item.provider}:${item.provider_id}`} item={item} downloadable />
          ))}
        </div>
      )}
    </>
  );
}

function getProviderCount(item: SearchResult): number {
  return "providers" in item && Array.isArray((item as { providers?: string[] }).providers)
    ? ((item as { providers: string[] }).providers.length)
    : 0;
}

function sortItems(items: SearchResult[], sort: SearchSort): SearchResult[] {
  if (sort === "relevance") return items;
  const sorted = [...items];
  sorted.sort((a, b) => {
    switch (sort) {
      case "title":
        return getTitle(a).localeCompare(getTitle(b));
      case "year_desc":
        return (getYear(b) ?? 0) - (getYear(a) ?? 0);
      case "year_asc":
        return (getYear(a) ?? 9999) - (getYear(b) ?? 9999);
      case "providers":
        return getProviderCount(b) - getProviderCount(a);
      default:
        return 0;
    }
  });
  return sorted;
}

export function SearchPage() {
  // Searching is for viewers with the `search` capability; a guest gets the
  // showcase instead of a request that would 401.
  return useCan("search") ? <SearchContent /> : <SignInToSearch />;
}

function SearchContent() {
  const { q: urlQ, type: searchType, providers: selectedProviders } = useSearch({ from: "/search" });
  const navigate = useNavigate({ from: "/search" });
  const { t } = useI18n();

  const [query, setQueryLocal] = useState(urlQ);
  const [debouncedQuery] = useDebounce(query, 400);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [sort, setSort] = useState<SearchSort>("relevance");

  useEffect(() => {
    if (debouncedQuery !== urlQ) {
      navigate({ search: (prev) => ({ ...prev, q: debouncedQuery }), replace: true });
    }
  }, [debouncedQuery]); // eslint-disable-line react-hooks/exhaustive-deps

  const setSearchType = (type: string) =>
    navigate({ search: (prev) => ({ ...prev, type: type as SearchType }), replace: true });

  const setProvider = (name: string) =>
    navigate({
      search: (prev) => ({
        ...prev,
        providers: name ? [name] : [],
      }),
      replace: true,
    });

  const activeProviders = selectedProviders.length > 0 ? selectedProviders : undefined;

  useEffect(() => { setVisibleCount(PAGE_SIZE); }, [debouncedQuery, searchType, activeProviders]);

  const { metadata: metadataProviders } = usePublicProviders();

  const chips = useMemo(() => {
    const list: FilterChip[] = [
      {
        key: "type",
        label: t("home.allTypes"),
        options: TYPE_OPTIONS.map((o) => ({ value: o.value, label: t(o.labelKey) })),
        value: searchType,
        onChange: setSearchType,
      },
    ];
    if (metadataProviders.length > 1) {
      list.push({
        key: "provider",
        label: t("home.allProviders"),
        options: metadataProviders.map((p) => ({ value: p.id, label: p.label })),
        value: selectedProviders.length === 1 ? selectedProviders[0] : "",
        onChange: setProvider,
      });
    }
    return list;
  }, [searchType, metadataProviders, selectedProviders, t]); // eslint-disable-line react-hooks/exhaustive-deps

  const sortOptions: SortOption<SearchSort>[] = useMemo(
    () => SORT_OPTIONS.map((o) => ({ value: o.value, label: t(o.labelKey) })),
    [t],
  );

  const { data, isLoading, isError } = useQuery({
    queryKey: ["search", debouncedQuery, searchType, activeProviders],
    queryFn: () => search(debouncedQuery, searchType, activeProviders),
    enabled: debouncedQuery.trim().length > 0,
    staleTime: SEARCH_STALE_MS,
  });

  const sortedItems = useMemo(
    () => sortItems(data?.items ?? [], sort),
    [data?.items, sort],
  );

  const loadMore = useCallback(() => setVisibleCount((c) => c + PAGE_SIZE), []);

  // Pulisce la barra di ricerca e riporta al Discovery.
  const clearSearch = useCallback(() => {
    setQueryLocal("");
    navigate({ search: (prev) => ({ ...prev, q: "" }), replace: true });
  }, [navigate]);

  // Home unificata: con una ricerca attiva mostra i risultati; a vuoto mostra
  // i contenuti di scoperta (new releases / trending). I filtri restano sempre
  // visibili, così si possono impostare le opzioni prima ancora di scrivere.
  const hasQuery = debouncedQuery.trim().length > 0;
  // Ultima pagina raggiunta (o nessun risultato): mostriamo il pulsante per
  // pulire la ricerca e tornare al Discovery in fondo alla lista.
  const atResultsEnd = !!data && !isLoading && visibleCount >= sortedItems.length;

  return (
    <div className="space-y-4">
      <h1 className="sr-only">{t("nav.search")}</h1>
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
        <Input
          type="text"
          placeholder={t("home.searchPlaceholder")}
          value={query}
          onChange={(e) => setQueryLocal(e.target.value)}
          className="pl-10 pr-10 h-11"
          autoFocus
        />
        {query && (
          <button
            type="button"
            onClick={clearSearch}
            title={t("home.clearSearch")}
            aria-label={t("home.clearSearch")}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground transition-colors hover:text-foreground"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      <ListToolbar<SearchSort>
        chips={chips}
        sort={{ value: sort, onChange: setSort, options: sortOptions }}
      />

      {hasQuery ? (
        <>
          {isError && (
            <p className="text-destructive text-sm">{t("home.searchFailed")}</p>
          )}

          <IncompleteResults errors={data?.provider_errors} />

          {data && data.items.length === 0 && (
            <p className="text-muted-foreground text-sm">{t("home.noResults", { query: debouncedQuery })}</p>
          )}

          {isLoading ? (
            <ContentList<SearchResult> items={[]} renderItem={() => null} keyExtractor={() => ""} loading skeletonCount={6} skeletonHeight="h-14" />
          ) : (
            <SearchResults items={sortedItems.slice(0, visibleCount)} />
          )}
          <LoadMoreButton visible={visibleCount} total={sortedItems.length} onLoadMore={loadMore} />

          {atResultsEnd && (
            <div className="flex justify-center pt-2">
              <Button variant="outline" size="sm" onClick={clearSearch}>
                <Compass className="mr-1.5 h-4 w-4" />
                {t("home.backToDiscovery")}
              </Button>
            </div>
          )}
        </>
      ) : (
        <DiscoverContent />
      )}
    </div>
  );
}
