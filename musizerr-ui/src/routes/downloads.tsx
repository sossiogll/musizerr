import { memo, useCallback, useId, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useDebounce } from "use-debounce";
import { ChevronDown, Download, Loader2, RotateCcw, Search } from "lucide-react";
import { getDownloads, retryDownload, type DownloadOptions, type DownloadPhase } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useAuth } from "@/contexts/AuthContext";
import { ApiErrorMessage, ErrorMessage } from "@/components/ErrorMessage";
import { FilterTabs } from "@/components/FilterTabs";
import { EmptyState, LoadMoreButton } from "@/components/ContentList";
import { TrackRow } from "@/components/tracks/TrackRow";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useWaitingCount } from "@/hooks/useDownloadApproval";
import { useDownloadsSummary } from "@/hooks/useDownloadsSummary";
import { useDownloadStates } from "@/hooks/useDownloadStates";
import { useRetryFailed } from "@/hooks/useRetryFailed";
import { useServerAction } from "@/hooks/useServerAction";
import { usePublicProviders } from "@/hooks/usePublicProviders";
import { useI18n, type MessageKey } from "@/i18n";
import { cn } from "@/lib/utils";
import { absoluteTime } from "@/lib/dates";
import type { DownloadAttempt, Track, TrackDownload } from "@/types";
import { DOWNLOADS_TABS, type DownloadsTab } from "./downloads-search";
import { MyRequestsPanel } from "./downloads-requests";

/** The tabs of the page that list tracks; each shows the tracks whose aggregated state is in these phases. */
const TAB_PHASES: Record<Exclude<DownloadsTab, "all" | "requests">, DownloadPhase[]> = {
  active: ["active"],
  queued: ["queued"],
  review: ["failed", "review"],
  done: ["done"],
};

const TAB_LABELS: Record<DownloadsTab, MessageKey> = {
  all: "downloads.tabAll",
  active: "downloads.tabActive",
  queued: "downloads.tabQueued",
  review: "downloads.tabReview",
  done: "downloads.tabDone",
  requests: "downloads.tabRequests",
};

// With "All", the page is split in these groups, most urgent first.
const GROUPS: { phases: DownloadPhase[]; titleKey: MessageKey }[] = [
  { phases: ["active", "queued"], titleKey: "downloads.sectionActive" },
  { phases: ["failed", "review"], titleKey: "downloads.sectionReview" },
  { phases: ["done"], titleKey: "downloads.sectionDone" },
];

const PAGE = 25;

export function DownloadsPage() {
  const search = useSearch({ from: "/downloads" });
  const navigate = useNavigate({ from: "/downloads" });
  const { t } = useI18n();
  const { authConfig } = useAuth();
  const [filter, setFilter] = useState("");
  const [q] = useDebounce(filter.trim(), 400);
  const { data: summary } = useDownloadsSummary();
  const waiting = useWaitingCount();
  const server = useServerAction();
  const retryAll = useRetryFailed();

  // The Requests tab exists for a viewer the approval policy governs: nobody else has requests.
  const governed = authConfig?.download_policy?.applies === true;
  const tab = search.tab === "requests" && !governed ? "all" : search.tab;

  const count = (phases: DownloadPhase[]) => (summary ? phases.reduce((n, p) => n + summary.phases[p], 0) : undefined);
  const tabs = DOWNLOADS_TABS.filter((value) => value !== "requests" || governed).map((value) => ({
    value,
    label: t(TAB_LABELS[value]),
    count: value === "all" ? summary?.total : value === "requests" ? waiting : count(TAB_PHASES[value]),
  }));
  const failed = count(["failed"]) ?? 0;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-bold max-sm:sr-only">{t("downloads.title")}</h1>
        {tab !== "requests" && (
          <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
            <label className="relative min-w-0 flex-1 sm:w-64 sm:flex-none">
              <span className="sr-only">{t("downloads.filterPlaceholder")}</span>
              <Search aria-hidden className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                type="search"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder={t("downloads.filterPlaceholder")}
                className="h-11 pl-9 sm:h-10"
              />
            </label>
            {failed > 0 && (
              <Button
                variant="outline"
                className="h-10"
                onClick={() => retryAll.mutate()}
                disabled={retryAll.isPending || server.disabled}
                title={server.reason}
              >
                {retryAll.isPending ? <Loader2 aria-hidden className="animate-spin" /> : <RotateCcw aria-hidden />}
                {t("downloads.retryAllFailed")}
              </Button>
            )}
          </div>
        )}
      </div>

      <FilterTabs tabs={tabs} value={tab} onChange={(value) => void navigate({ search: { tab: value }, replace: true })} label={t("downloads.tabsLabel")} />

      {tab === "requests" ? (
        <MyRequestsPanel />
      ) : summary?.total === 0 && !q ? (
        <EmptyState icon={Download} message={t("downloads.empty")} />
      ) : tab === "all" ? (
        GROUPS.map((group) => <DownloadGroup key={group.titleKey} phases={group.phases} titleKey={group.titleKey} q={q} />)
      ) : (
        <DownloadGroup phases={TAB_PHASES[tab]} q={q} />
      )}
    </div>
  );
}

/** One group of downloads (a set of phases), loaded page by page; titled with its own total. */
function DownloadGroup({ phases, titleKey, q }: { phases: DownloadPhase[]; titleKey?: MessageKey; q: string }) {
  const { t } = useI18n();
  const [limit, setLimit] = useState(PAGE);
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.downloads({ phases, q, limit }),
    queryFn: () => getDownloads({ phase: phases, q, limit, sort: "recent" }),
    placeholderData: (previous) => previous,
    // The progress stream refreshes these; a slow poll covers a stream that is down.
    refetchInterval: phases.some((p) => p === "active" || p === "queued") ? 15_000 : false,
  });
  const items = data?.items ?? [];
  const title = titleKey && t(titleKey, { count: data?.total ?? 0 });

  if (!data && error) {
    return <ApiErrorMessage className="py-8 text-center text-sm text-muted-foreground" error={error} fallback={t("errors.listFailed")} />;
  }
  if (title && !isLoading && items.length === 0) return null;

  return (
    <section className="space-y-2">
      {title && (data ? <h2 className="font-display text-lg font-semibold">{title}</h2> : <Skeleton className="h-7 w-40" />)}
      {isLoading ? (
        <div className="space-y-1">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-14" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-12 text-muted-foreground">
          <Download aria-hidden className="size-8 opacity-50" />
          <p className="text-sm">{q ? t("downloads.noMatch") : t("downloads.sectionEmpty")}</p>
        </div>
      ) : (
        <ul className="space-y-0.5">
          {items.map((item) => (
            <li key={item.track_id}>
              <DownloadItem item={item} />
            </li>
          ))}
        </ul>
      )}
      <LoadMoreButton visible={items.length} total={data?.total ?? 0} onLoadMore={() => setLimit((n) => n + PAGE)} />
    </section>
  );
}

/**
 * A downloaded (or wanted) track: the shared track row with its aggregated
 * state and one Retry, and its attempts behind a disclosure.
 */
const DownloadItem = memo(function DownloadItem({ item }: { item: TrackDownload }) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const { user } = useAuth();
  const { t } = useI18n();
  const track = useMemo(() => item.track ?? placeholderTrack(item.track_id), [item.track, item.track_id]);
  const retry = useCallback((options: DownloadOptions) => retryDownload(item.track_id, options), [item.track_id]);
  // Your own requests say nothing new; someone else's do.
  const requestedByOther = item.requested_by && item.requested_by !== user?.username ? item.requested_by : null;

  return (
    <div className={cn("rounded-md", open && "bg-muted/30")}>
      <div className="flex items-center">
        <div className="min-w-0 flex-1">
          <TrackRow track={track} statusOverride={item.status} retry={retry} />
        </div>
        {item.attempts.length > 0 && (
          <button
            type="button"
            aria-expanded={open}
            aria-controls={panelId}
            aria-label={t(open ? "downloads.hideAttempts" : "downloads.showAttempts", { title: track.title })}
            onClick={() => setOpen((v) => !v)}
            className="grid size-11 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ChevronDown aria-hidden className={cn("size-4 transition-transform", open && "rotate-180")} />
          </button>
        )}
      </div>
      {open && (
        <div id={panelId} className="space-y-2 px-3 pb-3 pt-1 text-sm">
          {requestedByOther && <p className="text-muted-foreground">{t("downloads.requestedByOther", { name: requestedByOther })}</p>}
          {item.file_path && <p className="break-all text-xs text-muted-foreground">{t("downloads.savedAs", { path: item.file_path })}</p>}
          <h3 className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t("downloads.attempts")}</h3>
          <ol className="space-y-1.5">
            {item.attempts.map((attempt) => (
              <AttemptRow key={attempt.download_id} attempt={attempt} />
            ))}
          </ol>
        </div>
      )}
    </div>
  );
});

function AttemptRow({ attempt }: { attempt: DownloadAttempt }) {
  const { t, locale } = useI18n();
  const { label } = useDownloadStates();
  const provider = usePublicProviders().label;
  const when = attempt.completed_at ?? attempt.started_at;
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1 text-xs">
      <span className="font-medium">{label(attempt.status)}</span>
      {attempt.provider_used && <span className="text-muted-foreground">{t("common.via", { provider: provider(attempt.provider_used) })}</span>}
      {when && (
        <time className="ml-auto text-muted-foreground" dateTime={when}>
          {absoluteTime(when, locale)}
        </time>
      )}
      {attempt.error_message && attempt.status !== "dispatched" && (
        <ErrorMessage className="w-full text-destructive" code={attempt.error_code} message={attempt.error_message} detail={attempt.error_detail} />
      )}
    </li>
  );
}

function placeholderTrack(trackId: string): Track {
  return {
    kind: "track",
    internal_id: trackId,
    title: "—",
    artist: "",
    album: "",
    albums: [],
    artists: [],
    duration_ms: 0,
    cover_url: null,
    preview_url: null,
    stream_url: null,
    provider: "",
    provider_id: "",
    artist_provider_id: "",
    album_provider_id: "",
    year: null,
    external_ids: [],
    providers: [],
  };
}
