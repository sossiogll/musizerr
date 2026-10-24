import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import type { LibraryCoverage as Coverage } from "@/types";

/**
 * How much of a release (or an artist) is in the library, as the server
 * computed it: "X of N in library · Y downloading · Z to review" over a bar.
 */
export function CoverageBar({ coverage, className }: { coverage: Coverage; className?: string }) {
  const { t } = useI18n();
  const { total, downloaded, in_progress, failed } = coverage;
  if (!total && !downloaded && !in_progress && !failed) return null;
  const denominator = Math.max(total ?? 0, downloaded + in_progress + failed, 1);
  const pct = (n: number) => `${(n / denominator) * 100}%`;
  return (
    <div className={cn("space-y-1.5", className)}>
      <div
        role="img"
        aria-label={coverageSentence(t, coverage)}
        className="flex h-1.5 w-full overflow-hidden rounded-xs bg-muted"
      >
        <span className="bg-success" style={{ width: pct(downloaded) }} />
        <span className="bg-primary" style={{ width: pct(in_progress) }} />
        <span className="bg-warning" style={{ width: pct(failed) }} />
      </div>
      <p aria-hidden className="text-xs text-muted-foreground tabular-nums">
        {coverageSentence(t, coverage)}
      </p>
    </div>
  );
}

type T = ReturnType<typeof useI18n>["t"];

export function coverageSentence(t: T, { total, downloaded, in_progress, failed }: Coverage): string {
  const parts = [total ? t("coverage.inLibraryOf", { count: downloaded, total }) : t("coverage.inLibrary", { count: downloaded })];
  if (in_progress) parts.push(t("coverage.downloading", { count: in_progress }));
  if (failed) parts.push(t("coverage.toReview", { count: failed }));
  return parts.join(" · ");
}

/** The short form for a tile: "3/10 in library", nothing when none is. */
export function CoverageShort({ coverage }: { coverage: Coverage | undefined }) {
  const { t } = useI18n();
  if (!coverage || (!coverage.downloaded && !coverage.in_progress && !coverage.failed)) return null;
  const complete = !!coverage.total && coverage.downloaded >= coverage.total;
  return (
    <span className={cn("tabular-nums", complete && "text-success")}>
      {coverage.total ? t("coverage.shortOf", { count: coverage.downloaded, total: coverage.total }) : t("coverage.short", { count: coverage.downloaded })}
      {coverage.failed > 0 && <span className="text-warning"> · {t("coverage.toReview", { count: coverage.failed })}</span>}
    </span>
  );
}
