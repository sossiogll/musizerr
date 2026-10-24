import type { LucideIcon } from "lucide-react";
import { RotateCcw } from "lucide-react";
import { EmptyState } from "@/components/ContentList";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { classifyError } from "@/lib/api-errors";

/**
 * A detail page (track, album, artist, playlist) that could not be loaded:
 * "not found" only when the server said 404; anything else (a provider
 * that failed, the API unreachable) says so in words, with Retry.
 */
export function DetailError({
  error,
  icon,
  notFound,
  onRetry,
  retrying = false,
}: {
  error: unknown;
  icon: LucideIcon;
  notFound: string;
  onRetry: () => void;
  retrying?: boolean;
}) {
  const { t } = useI18n();
  if (!error || classifyError(error).status === 404) return <EmptyState icon={icon} message={notFound} />;
  return (
    <div role="alert" className="flex flex-col items-center gap-3 py-12 text-center">
      <ApiErrorMessage className="max-w-md text-sm text-muted-foreground" error={error} fallback={t("errors.detailFailed")} />
      <Button variant="outline" onClick={onRetry} disabled={retrying}>
        <RotateCcw aria-hidden className={retrying ? "animate-spin" : undefined} />
        {t("common.retry")}
      </Button>
    </div>
  );
}
