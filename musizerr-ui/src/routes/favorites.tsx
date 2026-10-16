import { useId, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useDebounce } from "use-debounce";
import { toast } from "sonner";
import { ChevronDown, Download, Heart, Hourglass, Loader2, MoreHorizontal, RefreshCw, Trash2 } from "lucide-react";
import { deleteSavedTracks, getFavorites, getLikedElsewhere } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { absoluteTime } from "@/lib/dates";
import { useApproval, useWaitingRequest } from "@/hooks/useDownloadApproval";
import { useJobErrorText, useLibraryJob, useLibraryJobs } from "@/hooks/useLibraryJobs";
import { usePublicProviders } from "@/hooks/usePublicProviders";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n, type MessageKey } from "@/i18n";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState, PageNav } from "@/components/ContentList";
import { FilterTabs } from "@/components/FilterTabs";
import { ApiErrorMessage, ErrorMessage } from "@/components/ErrorMessage";
import { LibraryJobProgress } from "@/components/library/LibraryJobProgress";
import { ListToolbar, type FilterChip, type SortOption } from "@/components/ListToolbar";
import { TrackList } from "@/components/tracks/TrackList";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import type { LibraryJobRun, LikedElsewhereTrack, Track } from "@/types";

type FavoriteSort = "added_desc" | "added_asc" | "title" | "artist" | "year_desc" | "year_asc";
type ElsewhereSort = FavoriteSort | "synced_desc" | "synced_asc";
export type FavoritesView = "favorites" | "elsewhere";

const FAVORITE_SORTS: { value: FavoriteSort; labelKey: MessageKey }[] = [
  { value: "added_desc", labelKey: "favorites.addedNewest" },
  { value: "added_asc", labelKey: "favorites.addedOldest" },
  { value: "title", labelKey: "home.sortTitle" },
  { value: "artist", labelKey: "downloads.sortArtist" },
  { value: "year_desc", labelKey: "home.sortYearNewest" },
  { value: "year_asc", labelKey: "home.sortYearOldest" },
];

const ELSEWHERE_SORTS: { value: ElsewhereSort; labelKey: MessageKey }[] = [
  ...FAVORITE_SORTS.slice(0, 2),
  { value: "synced_desc", labelKey: "favorites.syncedNewest" },
  { value: "synced_asc", labelKey: "favorites.syncedOldest" },
  ...FAVORITE_SORTS.slice(2),
];

const PAGE_SIZE = 50;

/**
 * The viewer's favorites (tracks of the library they liked: the heart, a
 * download with favorite, a like from another service that matched the
 * library), and a second view with the likes from their other services that
 * are not in the library yet — each with its download.
 */
export function FavoritesPage({ view, onViewChange }: { view: FavoritesView; onViewChange: (view: FavoritesView) => void }) {
  const { t } = useI18n();
  const jobs = useLibraryJobs();
  const sync = useLibraryJob("library_sync");
  const reasonId = useId();
  const tabs = useMemo(
    () => [
      { value: "favorites" as const, label: t("favorites.tabFavorites") },
      { value: "elsewhere" as const, label: t("favorites.tabElsewhere") },
    ],
    [t],
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="font-display text-2xl font-bold max-sm:sr-only">{t("favorites.title")}</h1>
        <div className="flex items-center gap-2">
          {view === "elsewhere" && <DownloadMissingMenu />}
          <Button
            variant="outline"
            className="max-sm:size-11 max-sm:p-0"
            onClick={() => sync.start()}
            disabled={sync.disabled}
            aria-busy={sync.busy || undefined}
            aria-describedby={sync.reason ? reasonId : undefined}
            title={sync.reason ?? t("favorites.syncNow")}
          >
            {sync.busy ? <Loader2 aria-hidden className="motion-safe:animate-spin" /> : <RefreshCw aria-hidden />}
            <span className="max-sm:sr-only">{t("favorites.syncNow")}</span>
          </Button>
          {sync.reason && (
            <span id={reasonId} className="sr-only">
              {sync.reason}
            </span>
          )}
          <MoreMenu />
        </div>
      </div>

      <LibraryJobProgress jobs={jobs} />
      <FilterTabs tabs={tabs} value={view} onChange={onViewChange} label={t("favorites.title")} />
      {!sync.active && <SyncStatusLine run={sync.run} />}

      {view === "favorites" ? <FavoritesList /> : <LikedElsewhereList neverSynced={sync.run?.status === "never_run"} />}
    </div>
  );
}

function FavoritesList() {
  const { t } = useI18n();
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState("");
  const [q] = useDebounce(filter.trim(), 400);
  const [sort, setSort] = useState<FavoriteSort>("added_desc");
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.favorites({ page, q, sort }),
    queryFn: () => getFavorites(page, PAGE_SIZE, q || undefined, sort),
    placeholderData: (previous) => previous,
  });
  const sortOptions: SortOption<FavoriteSort>[] = useMemo(() => FAVORITE_SORTS.map((o) => ({ value: o.value, label: t(o.labelKey) })), [t]);

  return (
    <ListView<FavoriteSort>
      filter={filter}
      onFilter={(v) => {
        setFilter(v);
        setPage(1);
      }}
      sort={sort}
      onSort={(v) => {
        setSort(v);
        setPage(1);
      }}
      sortOptions={sortOptions}
      count={data ? t("favorites.trackCount", { count: data.total }) : undefined}
      loading={isLoading}
      error={!data ? error : null}
      tracks={data?.items ?? []}
      empty={q ? t("album.noMatch") : t("favorites.emptyFavorites")}
      label={t("favorites.tabFavorites")}
      page={page}
      pages={data?.pages ?? 0}
      onPage={setPage}
    />
  );
}

function LikedElsewhereList({ neverSynced }: { neverSynced: boolean }) {
  const { t } = useI18n();
  const [page, setPage] = useState(1);
  const [provider, setProvider] = useState("");
  const [filter, setFilter] = useState("");
  const [q] = useDebounce(filter.trim(), 400);
  const [sort, setSort] = useState<ElsewhereSort>("added_desc");
  const { metadata: metadataProviders } = usePublicProviders();
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.likedElsewhere({ page, provider, q, sort }),
    queryFn: () => getLikedElsewhere(page, PAGE_SIZE, provider || undefined, q || undefined, sort),
    placeholderData: (previous) => previous,
  });
  const tracks = useMemo(() => (data?.items ?? []).map(withLikedProviders), [data?.items]);
  const sortOptions: SortOption<ElsewhereSort>[] = useMemo(() => ELSEWHERE_SORTS.map((o) => ({ value: o.value, label: t(o.labelKey) })), [t]);
  const chips = useMemo<FilterChip[]>(
    () =>
      metadataProviders.length > 1
        ? [
            {
              key: "provider",
              label: t("home.allProviders"),
              options: metadataProviders.map((p) => ({ value: p.id, label: p.label })),
              value: provider,
              onChange: (v) => {
                setProvider(v);
                setPage(1);
              },
            },
          ]
        : [],
    [metadataProviders, provider, t],
  );

  return (
    <>
      <p className="text-sm text-muted-foreground">{t("favorites.elsewhereHint")}</p>
      <ListView<ElsewhereSort>
        filter={filter}
        onFilter={(v) => {
          setFilter(v);
          setPage(1);
        }}
        chips={chips}
        sort={sort}
        onSort={(v) => {
          setSort(v);
          setPage(1);
        }}
        sortOptions={sortOptions}
        loading={isLoading}
        error={!data ? error : null}
        tracks={tracks}
        empty={q ? t("album.noMatch") : neverSynced ? t("favorites.emptyNeverSynced") : t("favorites.emptyElsewhere")}
        label={t("favorites.tabElsewhere")}
        page={page}
        pages={data?.pages ?? 0}
        onPage={setPage}
      />
    </>
  );
}

function ListView<S extends string>({
  filter,
  onFilter,
  chips,
  sort,
  onSort,
  sortOptions,
  count,
  loading,
  error,
  tracks,
  empty,
  label,
  page,
  pages,
  onPage,
}: {
  filter: string;
  onFilter: (value: string) => void;
  chips?: FilterChip[];
  sort: S;
  onSort: (value: S) => void;
  sortOptions: SortOption<S>[];
  count?: string;
  loading: boolean;
  error: unknown;
  tracks: Track[];
  empty: string;
  label: string;
  page: number;
  pages: number;
  onPage: (page: number) => void;
}) {
  const { t } = useI18n();
  return (
    <>
      {count && <p className="text-sm text-muted-foreground">{count}</p>}
      <ListToolbar<S>
        filter={{ value: filter, onChange: onFilter, placeholder: t("downloads.filterPlaceholder") }}
        chips={chips && chips.length > 0 ? chips : undefined}
        sort={{ value: sort, onChange: onSort, options: sortOptions }}
      />
      {loading ? (
        <div aria-hidden className="space-y-1">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-14" />
          ))}
        </div>
      ) : error ? (
        // Never loaded and not in the saved copy: not the same as an empty list.
        <ApiErrorMessage className="py-12 text-center text-sm text-muted-foreground" error={error} fallback={t("errors.listFailed")} />
      ) : tracks.length === 0 ? (
        <EmptyState icon={Heart} message={empty} />
      ) : (
        <TrackList tracks={tracks} header label={label} />
      )}
      <PageNav page={page} pages={pages} onPageChange={onPage} />
    </>
  );
}

/** The last sync, when none is running: its date, or why it failed (in words, never the raw error). */
function SyncStatusLine({ run }: { run: LibraryJobRun | undefined }) {
  const { t, locale } = useI18n();
  const errorText = useJobErrorText();
  if (!run || run.status === "never_run") return null;
  if (run.status === "failed") {
    return (
      <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
        {/* Already in words (and actionable for an admin): no code to translate again. */}
        <ErrorMessage code={null} message={errorText(run)} detail={run.error_detail} />
      </div>
    );
  }
  return run.completed_at ? (
    <p className="text-sm text-muted-foreground">{t("favorites.lastSync", { date: absoluteTime(run.completed_at, locale) })}</p>
  ) : null;
}

/**
 * "Download the ones liked elsewhere": queues the likes not in the library
 * yet, over a period. Its progress is in the page's progress area.
 */
function DownloadMissingMenu() {
  const { t } = useI18n();
  const job = useLibraryJob("download_sync");
  const reasonId = useId();
  // How many likes it would take is not known before the job looks.
  const approval = useApproval("liked_elsewhere", null);
  const waiting = useWaitingRequest("liked_elsewhere");

  if (waiting) {
    return (
      <Badge variant="warning" role="status" className="h-11 gap-1.5 rounded-md px-4 text-sm">
        <Hourglass aria-hidden className="size-4" />
        {t("approval.waitingTracks", { count: waiting.needed_tracks })}
      </Badge>
    );
  }
  // They are the viewer's likes: they arrive as favorites (the server always adds them).
  const label = t(approval.waits ? "approval.requestLikesItem" : "favorites.downloadMissingUser");

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            className="max-sm:size-11 max-sm:p-0"
            disabled={job.disabled}
            aria-busy={job.busy || undefined}
            aria-describedby={job.reason ? reasonId : undefined}
            aria-label={label}
            title={job.reason ?? approval.hint ?? label}
          >
            {job.busy ? <Loader2 aria-hidden className="motion-safe:animate-spin" /> : <Download aria-hidden />}
            <span className="max-sm:sr-only">{t(approval.waits ? "approval.requestLikes" : "favorites.downloadMissing")}</span>
            <ChevronDown aria-hidden className="max-sm:hidden" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => job.start("all")}>{t("favorites.allSavedTracks")}</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => job.start("1m")}>{t("favorites.lastMonth")}</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => job.start("3m")}>{t("favorites.last3Months")}</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {job.reason && (
        <span id={reasonId} className="sr-only">
          {job.reason}
        </span>
      )}
    </>
  );
}

/** The rare, destructive actions, out of the way and behind a confirmation. */
function MoreMenu() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const [confirming, setConfirming] = useState(false);
  const mutation = useMutation({
    mutationFn: deleteSavedTracks,
    onSuccess: () => {
      toast.success(t("favorites.dataDeleted"));
      void queryClient.invalidateQueries({ queryKey: queryKeys.likedElsewhere() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.libraryJobs });
    },
    onError: () => toast.error(t("favorites.deleteFailed")),
  });

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="ghost" size="icon" className="size-11" aria-label={t("favorites.moreActions")}>
            {mutation.isPending ? <Loader2 aria-hidden className="animate-spin" /> : <MoreHorizontal aria-hidden />}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            className="text-destructive focus:text-destructive"
            disabled={mutation.isPending || server.disabled}
            onSelect={() => setConfirming(true)}
          >
            <Trash2 aria-hidden className="size-4" />
            {t("favorites.deleteLibraryData")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={t("favorites.deleteLibraryData")}
        description={t("favorites.deleteConfirm")}
        confirmLabel={t("favorites.deleteAction")}
        onConfirm={() => mutation.mutate()}
        destructive
      />
    </>
  );
}

/** A like from elsewhere as a Track: its providers are the ones it was liked on when the record has none. */
function withLikedProviders(liked: LikedElsewhereTrack): Track {
  if (liked.providers?.length) return liked;
  return { ...liked, providers: Object.keys(liked.provider_saved_at ?? {}) };
}
