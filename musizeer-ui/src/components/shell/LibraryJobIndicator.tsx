import { Link } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { ProgressBar } from "@/components/ProgressBar";
import { JOB_KINDS, isJobActive, jobPercent, useJobStatusText, useJobTitle, useLibraryJobs } from "@/hooks/useLibraryJobs";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import type { LibraryJobRun } from "@/types";

/**
 * The shell's compact sign of a library job in progress, visible from every
 * page: a spinner (still with reduced motion), and, with `withText`, the
 * phase and a thin bar. A link to the library, where the details are.
 * Nothing while no job is queued or running.
 */
export function LibraryJobIndicator({ withText = false, className }: { withText?: boolean; className?: string }) {
  const jobs = useLibraryJobs();
  const { t } = useI18n();
  const title = useJobTitle();
  const statusText = useJobStatusText();
  const run = JOB_KINDS.map((kind) => jobs?.[kind]).find((r): r is LibraryJobRun => isJobActive(r));
  if (!run) return null;
  const text = t("jobs.indicator", { title: title(run.kind), status: statusText(run) });
  return (
    <Link
      to="/favorites"
      title={text}
      aria-label={text}
      className={cn(
        "inline-flex min-w-0 items-center gap-2 rounded-md text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        withText ? "w-full px-2 py-1.5" : "size-11 justify-center",
        className,
      )}
    >
      <Loader2 aria-hidden className="size-4 shrink-0 text-primary motion-safe:animate-spin" />
      {withText && (
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="truncate">{statusText(run)}</span>
          <ProgressBar className="h-1" percent={jobPercent(run)} label={t("jobs.progressLabel", { title: title(run.kind) })} />
        </span>
      )}
    </Link>
  );
}
