import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { getLibraryJobs, triggerLibraryDownloadSync, triggerLibrarySync } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useErrorText, useFailureToast } from "@/lib/error-text";
import { useProgressEvents } from "@/contexts/QueueContext";
import { useCapabilities } from "@/hooks/useCapabilities";
import { usePublicProviders } from "@/hooks/usePublicProviders";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n, type MessageKey } from "@/i18n";
import type { LibraryJobKind, LibraryJobRun, LibraryJobs, LibraryJobStarted, StreamEvent } from "@/types";

/** How often the state is read while a job is queued or running: the
 *  fallback of the progress stream, and what notices a run the server closed
 *  as stale (the stream has no event for a worker that never came). */
export const JOB_POLL_MS = 4_000;

export const JOB_KINDS: readonly LibraryJobKind[] = ["library_sync", "download_sync"];

export const isJobActive = (run: LibraryJobRun | undefined): boolean => run?.status === "queued" || run?.status === "running";

const TITLE: Record<LibraryJobKind, MessageKey> = {
  library_sync: "jobs.syncTitle",
  download_sync: "jobs.downloadTitle",
};

const IN_PROGRESS: Record<LibraryJobKind, MessageKey> = {
  library_sync: "jobs.syncInProgress",
  download_sync: "jobs.downloadInProgress",
};

/** Keeps a run the server already finished from being overwritten by an older event. */
function merge(current: LibraryJobs | undefined, run: LibraryJobRun): LibraryJobs | undefined {
  if (!current) return current;
  const previous = current[run.kind];
  if (previous?.id === run.id && !isJobActive(previous) && previous.status !== "never_run" && isJobActive(run)) return current;
  return { ...current, [run.kind]: run };
}

export function setJobRun(queryClient: QueryClient, run: LibraryJobRun) {
  queryClient.setQueryData<LibraryJobs>(queryKeys.libraryJobs, (current) => merge(current, run));
}

/**
 * The viewer's library jobs, as the server decides them (GET /library/jobs):
 * updated live by the progress stream, and read again every few seconds
 * while one is queued or running. One query shared by the page and the shell.
 */
export function useLibraryJobs() {
  const { can } = useCapabilities();
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: queryKeys.libraryJobs,
    queryFn: getLibraryJobs,
    enabled: can.favorite,
    refetchInterval: (query) => (JOB_KINDS.some((kind) => isJobActive(query.state.data?.[kind])) ? JOB_POLL_MS : false),
  });
  const onEvent = useCallback(
    (event: StreamEvent) => {
      if (event.type === "library_job") setJobRun(queryClient, event.run);
    },
    [queryClient],
  );
  useProgressEvents(onEvent);
  return data;
}

/**
 * One job for its button: the run, starting it (idempotent on the server: a
 * second click gets the run already active), and why the button is disabled.
 */
export function useLibraryJob(kind: LibraryJobKind) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const failed = useFailureToast();
  const server = useServerAction();
  const run = useLibraryJobs()?.[kind];
  const mutation = useMutation<LibraryJobStarted, unknown, "all" | "1m" | "3m" | undefined>({
    mutationFn: (range) => (kind === "library_sync" ? triggerLibrarySync() : triggerLibraryDownloadSync(range ?? "all")),
    onSuccess: (started) => {
      const { created, ...current } = started;
      setJobRun(queryClient, current);
      if (!created) toast.info(t(IN_PROGRESS[kind]));
    },
    onError: (error) => {
      failed(error, t(kind === "library_sync" ? "favorites.syncStartFailed" : "favorites.downloadSyncStartFailed"));
      void queryClient.invalidateQueries({ queryKey: queryKeys.libraryJobs });
    },
  });
  const active = isJobActive(run);
  const reason =
    server.reason ??
    (mutation.isPending ? t("jobs.starting") : run?.status === "queued" ? t("jobs.waitingForWorker") : active ? t(IN_PROGRESS[kind]) : undefined);
  return {
    run,
    active,
    busy: active || mutation.isPending,
    start: (range?: "all" | "1m" | "3m") => mutation.mutate(range),
    disabled: active || mutation.isPending || server.disabled,
    reason,
  };
}

/** The title of a job ("Syncing likes"). */
export function useJobTitle() {
  const { t } = useI18n();
  return useCallback((kind: LibraryJobKind) => t(TITLE[kind]), [t]);
}

/** What an active run is doing, in words, with the providers' labels. */
export function useJobStatusText() {
  const { t } = useI18n();
  const { label } = usePublicProviders();
  return useCallback(
    (run: LibraryJobRun): string => {
      if (run.status === "queued") return t("jobs.waitingForWorker");
      const provider = run.provider ? label(run.provider) : "";
      const processed = run.processed ?? 0;
      const total = run.total ?? null;
      switch (run.phase) {
        case "fetching":
          return processed > 0 ? t("jobs.fetchingCount", { provider, count: processed }) : t("jobs.fetching", { provider });
        case "saving":
          if (total === null) return t("jobs.fetching", { provider });
          return provider ? t("jobs.savingFrom", { provider, processed, total }) : t("jobs.saving", { processed, total });
        case "preparing":
          return t("jobs.preparing");
        case "checking":
          return total === null ? t("jobs.preparing") : t("jobs.checking", { processed, total });
        default:
          return t("jobs.starting");
      }
    },
    [t, label],
  );
}

/** The readable error of a failed run; "the worker isn't running" is actionable for an admin only. */
export function useJobErrorText() {
  const { t } = useI18n();
  const { can } = useCapabilities();
  const errorText = useErrorText();
  return useCallback(
    (run: LibraryJobRun): string => {
      if (run.error_code === "worker_unavailable" && can.admin) return t("jobs.workerUnavailableAdmin");
      return errorText(run.error_code, run.error_message ?? t(run.kind === "library_sync" ? "jobs.syncFailed" : "jobs.downloadFailed"));
    },
    [t, can.admin, errorText],
  );
}

/** The progress of a run, 0–100, or undefined when its total is not known. */
export function jobPercent(run: LibraryJobRun): number | undefined {
  if (run.status !== "running" || run.total === null || run.total === undefined) return undefined;
  if (run.phase !== "saving" && run.phase !== "checking") return undefined;
  if (run.total === 0) return 100;
  return Math.min(100, Math.max(0, ((run.processed ?? 0) / run.total) * 100));
}
