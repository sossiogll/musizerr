import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { TrendingUp } from "lucide-react";
import {
  getNewReleases,
  getTrendingAlbums,
  getTrendingTracks,
  getTrendingArtists,
} from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useI18n } from "@/i18n";
import { IncompleteResults, mergeProviderErrors } from "@/components/IncompleteResults";
import { LoadMoreButton } from "@/components/ContentList";
import { TileShelf } from "@/components/TileShelf";
import { TrackList } from "@/components/tracks/TrackList";
import { Skeleton } from "@/components/ui/skeleton";
import type { Track } from "@/types";

const STALE_MS = 10 * 60 * 1000;
const FETCHED = 20;
const TRACKS_SHOWN = 10;

/** New releases and what is trending, from every metadata provider. */
export function DiscoverContent() {
  const { t } = useI18n();
  const newReleases = useQuery({
    queryKey: queryKeys.discover("new-releases"),
    queryFn: () => getNewReleases(FETCHED),
    staleTime: STALE_MS,
  });
  const trendingTracks = useQuery({
    queryKey: queryKeys.discover("trending-tracks"),
    queryFn: () => getTrendingTracks(FETCHED),
    staleTime: STALE_MS,
  });
  const trendingAlbums = useQuery({
    queryKey: queryKeys.discover("trending-albums"),
    queryFn: () => getTrendingAlbums(FETCHED),
    staleTime: STALE_MS,
  });
  const trendingArtists = useQuery({
    queryKey: queryKeys.discover("trending-artists"),
    queryFn: () => getTrendingArtists(FETCHED),
    staleTime: STALE_MS,
  });

  const sections = [newReleases, trendingTracks, trendingAlbums, trendingArtists];
  const shows = (q: (typeof sections)[number]) => q.isLoading || (q.data?.items.length ?? 0) > 0;
  // A section says itself when its data is incomplete; a section that is not
  // shown (nothing came back) has no place for that, so the page says it.
  const hiddenErrors = mergeProviderErrors(sections.filter((q) => !shows(q)).map((q) => q.data?.provider_errors));
  const incomplete = (q: (typeof sections)[number]) => <IncompleteResults variant="inline" errors={q.data?.provider_errors} />;

  return (
    <div className="space-y-10">
      <IncompleteResults errors={hiddenErrors} />
      {!sections.some(shows) && (
        <div className="space-y-2 py-12 text-center">
          <TrendingUp aria-hidden className="mx-auto size-10 text-muted-foreground/40" />
          <p className="text-sm text-muted-foreground">{t("discover.empty")}</p>
        </div>
      )}

      {shows(newReleases) && (
        <Section title={t("discover.newReleases")}>
          <TileShelf items={newReleases.data?.items} loading={newReleases.isLoading} label={t("discover.newReleases")} downloadable />
          {incomplete(newReleases)}
        </Section>
      )}

      {shows(trendingTracks) && (
        <Section title={t("discover.trendingTracks")}>
          {trendingTracks.isLoading ? (
            <div aria-hidden className="space-y-1">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-14" />
              ))}
            </div>
          ) : (
            <TrendingTracks tracks={trendingTracks.data?.items ?? []} label={t("discover.trendingTracks")} />
          )}
          {incomplete(trendingTracks)}
        </Section>
      )}

      {shows(trendingAlbums) && (
        <Section title={t("discover.trendingAlbums")}>
          <TileShelf items={trendingAlbums.data?.items} loading={trendingAlbums.isLoading} label={t("discover.trendingAlbums")} downloadable />
          {incomplete(trendingAlbums)}
        </Section>
      )}

      {shows(trendingArtists) && (
        <Section title={t("discover.trendingArtists")}>
          <TileShelf items={trendingArtists.data?.items} loading={trendingArtists.isLoading} label={t("discover.trendingArtists")} />
          {incomplete(trendingArtists)}
        </Section>
      )}
    </div>
  );
}

function TrendingTracks({ tracks, label }: { tracks: Track[]; label: string }) {
  const [shown, setShown] = useState(TRACKS_SHOWN);
  return (
    <>
      <TrackList tracks={tracks.slice(0, shown)} numbering="index" header label={label} />
      <LoadMoreButton visible={shown} total={tracks.length} onLoadMore={() => setShown(tracks.length)} />
    </>
  );
}

export function Section({ title, action, children }: { title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-display text-lg font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}
