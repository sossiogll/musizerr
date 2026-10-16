import { useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAnnouncePending } from "@/hooks/useDownloadApproval";
import { JOB_KINDS, isJobActive, useJobErrorText, useLibraryJobs } from "@/hooks/useLibraryJobs";
import { useI18n } from "@/i18n";
import { queryKeys } from "@/lib/query-keys";
import type { LibraryJobKind, LibraryJobRun } from "@/types";

/**
 * Mounted once by the shell: when a library job the viewer saw queued or
 * running ends, a toast says how it went (from any page) and the lists it
 * changed are read again. A run already finished when the app opens is not
 * announced.
 */
export function LibraryJobsWatcher() {
  const jobs = useLibraryJobs();
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const errorText = useJobErrorText();
  const announcePending = useAnnouncePending();
  const seen = useRef<Partial<Record<LibraryJobKind, LibraryJobRun>>>({});

  useEffect(() => {
    if (!jobs) return;
    for (const kind of JOB_KINDS) {
      const run = jobs[kind];
      const previous = seen.current[kind];
      seen.current[kind] = run;
      if (!previous || !isJobActive(previous) || isJobActive(run) || run.status === "never_run") continue;
      if (previous.id !== run.id) continue;
      announce(run);
    }

    function announce(run: LibraryJobRun) {
      if (run.status === "failed") {
        toast.error(errorText(run));
        return;
      }
      if (run.kind === "library_sync") {
        void queryClient.invalidateQueries({ queryKey: queryKeys.favorites() });
        void queryClient.invalidateQueries({ queryKey: queryKeys.likedElsewhere() });
        if (run.providers?.some((p) => p.status === "failed")) toast.warning(t("jobs.syncPartial"));
        else toast.success(t("jobs.syncComplete", { count: run.result?.tracks_synced ?? 0 }));
        return;
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.downloads() });
      if (run.result?.pending_approval) {
        // Nothing was queued: the likes download is a request that waits for an admin.
        announcePending(run.result.pending_approval);
        return;
      }
      const queued = run.result?.queued ?? 0;
      if (queued > 0) toast.success(t("favorites.tracksQueuedForDownload", { count: queued }));
      else toast.info(t("favorites.allTracksAvailable"));
    }
  }, [jobs, queryClient, t, errorText, announcePending]);

  return null;
}
