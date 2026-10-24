import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { AlertTriangle, CheckCircle2, Loader2, RotateCcw, Download as DownloadIcon, type LucideIcon } from "lucide-react";
import { useCan } from "@/hooks/useCapabilities";
import { useDownloadsSummary } from "@/hooks/useDownloadsSummary";
import { useRetryFailed } from "@/hooks/useRetryFailed";
import { useServerAction } from "@/hooks/useServerAction";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import { DiscoverContent } from "./discover";

export function HomePage() {
  const { t } = useI18n();
  const canDownload = useCan("download");
  return (
    <div className="space-y-10">
      <h1 className="font-display text-2xl font-bold max-sm:sr-only">{t("nav.home")}</h1>
      {canDownload && <DownloadStatusCards />}
      <DiscoverContent />
    </div>
  );
}

/**
 * The state of the viewer's downloads at a glance: what is on its way, what
 * needs a look (with "Retry all") and what arrived this week. Each card
 * opens the matching Downloads tab.
 */
function DownloadStatusCards() {
  const { t } = useI18n();
  const { data, isLoading } = useDownloadsSummary();
  const retryAll = useRetryFailed();
  const server = useServerAction();

  if (isLoading) {
    return (
      <div aria-hidden className="grid grid-cols-3 gap-2 sm:gap-3">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-20 rounded-lg sm:h-28" />
        ))}
      </div>
    );
  }
  if (!data) return null;

  const inProgress = data.phases.active + data.phases.queued;
  const toReview = data.phases.failed + data.phases.review;

  return (
    <section aria-label={t("homeStatus.label")} className="grid grid-cols-3 gap-2 sm:gap-3">
      <StatusCard
        icon={DownloadIcon}
        count={inProgress}
        label={t("homeStatus.inProgress")}
        tab={data.phases.active > 0 ? "active" : "queued"}
        tone={inProgress > 0 ? "text-primary" : undefined}
      />
      <StatusCard
        icon={AlertTriangle}
        count={toReview}
        label={t("homeStatus.toReview")}
        tab="review"
        tone={toReview > 0 ? "text-warning" : undefined}
        action={
          data.phases.failed > 0 && (
            <Button
              variant="outline"
              size="sm"
              className="max-sm:size-11 max-sm:p-0"
              onClick={() => retryAll.mutate()}
              disabled={retryAll.isPending || server.disabled}
              title={server.reason ?? t("homeStatus.retryAll")}
            >
              {retryAll.isPending ? <Loader2 aria-hidden className="animate-spin" /> : <RotateCcw aria-hidden />}
              <span className="max-sm:sr-only">{t("homeStatus.retryAll")}</span>
            </Button>
          )
        }
      />
      <StatusCard
        icon={CheckCircle2}
        count={data.downloaded_since}
        label={t("homeStatus.addedThisWeek")}
        tab="done"
        tone={data.downloaded_since > 0 ? "text-success" : undefined}
      />
    </section>
  );
}

function StatusCard({
  icon: Icon,
  count,
  label,
  tab,
  tone,
  action,
}: {
  icon: LucideIcon;
  count: number;
  label: string;
  tab: "active" | "queued" | "review" | "done";
  tone?: string;
  action?: ReactNode;
}) {
  return (
    // The whole card opens the tab (a stretched link); the action sits above it.
    // Three across even on a phone: compact there, roomier from S up.
    <div className="relative flex min-w-0 flex-col justify-between gap-2 rounded-lg border bg-card p-3 transition-colors hover:bg-muted/40 focus-within:ring-2 focus-within:ring-ring sm:min-h-28 sm:gap-3 sm:p-4">
      <div className="flex items-start justify-between gap-2">
        <Link
          to="/downloads"
          search={{ tab }}
          className="min-w-0 after:absolute after:inset-0 after:rounded-lg focus-visible:outline-none"
        >
          <span className="block font-display text-2xl font-bold tabular-nums sm:text-3xl">{count}</span>
          <span className="block text-xs leading-tight text-muted-foreground sm:text-sm">{label}</span>
        </Link>
        <Icon aria-hidden className={cn("size-5 shrink-0 text-muted-foreground max-sm:hidden", tone)} />
      </div>
      {action && <div className="relative z-10 self-start">{action}</div>}
    </div>
  );
}
