import { Loader2 } from "lucide-react";
import { ProgressBar } from "@/components/ProgressBar";
import { JOB_KINDS, isJobActive, jobPercent, useJobStatusText, useJobTitle } from "@/hooks/useLibraryJobs";
import { useI18n } from "@/i18n";
import type { LibraryJobRun, LibraryJobs } from "@/types";

/**
 * The library page's progress area: one panel per job queued or running,
 * with its phase in words and a bar (determinate when the total is known).
 * Nothing when no job is active.
 */
export function LibraryJobProgress({ jobs }: { jobs: LibraryJobs | undefined }) {
  const active = JOB_KINDS.map((kind) => jobs?.[kind]).filter((run): run is LibraryJobRun => isJobActive(run));
  if (active.length === 0) return null;
  return (
    <div className="space-y-2">
      {active.map((run) => (
        <JobPanel key={run.kind} run={run} />
      ))}
    </div>
  );
}

function JobPanel({ run }: { run: LibraryJobRun }) {
  const { t } = useI18n();
  const title = useJobTitle()(run.kind);
  const status = useJobStatusText()(run);
  const providers = run.providers ?? [];
  const index = run.provider ? providers.findIndex((p) => p.provider === run.provider) : -1;
  return (
    <section aria-label={title} className="rounded-lg border bg-card px-3 py-2.5">
      <div className="flex items-center gap-2 text-sm">
        <Loader2 aria-hidden className="size-4 shrink-0 text-primary motion-safe:animate-spin" />
        <span className="font-medium">{title}</span>
        {providers.length > 1 && index >= 0 && (
          <span className="ml-auto text-xs text-muted-foreground tabular-nums">
            {t("jobs.providerStep", { index: index + 1, count: providers.length })}
          </span>
        )}
      </div>
      <p role="status" className="mt-1 truncate text-sm text-muted-foreground tabular-nums">
        {status}
      </p>
      <ProgressBar className="mt-2" percent={jobPercent(run)} label={t("jobs.progressLabel", { title })} />
    </section>
  );
}
