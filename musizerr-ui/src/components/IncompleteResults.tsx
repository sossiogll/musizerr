import { AlertTriangle } from "lucide-react";
import { useProviderLabel } from "@/components/ProviderBadges";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import type { ProviderError } from "@/types";

/** The failures of several aggregated responses, one per provider. */
export function mergeProviderErrors(lists: (ProviderError[] | undefined)[]): ProviderError[] {
  const byProvider = new Map<string, ProviderError>();
  for (const list of lists) for (const error of list ?? []) if (!byProvider.has(error.provider)) byProvider.set(error.provider, error);
  return [...byProvider.values()];
}

/**
 * "Results may be incomplete": the providers that failed while building an
 * aggregated response (its `provider_errors`). Renders nothing when none did.
 *
 * Shown where the incomplete data is, never page-wide: `banner` when the
 * response is the page's main content (search results), `inline` — a quiet
 * line — inside the one section it affects (related tracks, discography, ...).
 */
export function IncompleteResults({
  errors,
  variant = "banner",
  className,
}: {
  errors: ProviderError[] | undefined;
  variant?: "banner" | "inline";
  className?: string;
}) {
  const label = useProviderLabel();
  const { t, locale } = useI18n();
  if (!errors?.length) return null;
  const names = new Intl.ListFormat(locale, { type: "conjunction" }).format(errors.map((e) => label(e.provider)));
  if (variant === "inline") {
    return (
      <p role="status" className={cn("flex items-start gap-1.5 text-xs text-muted-foreground", className)}>
        <AlertTriangle aria-hidden className="mt-px size-3.5 shrink-0 text-warning" />
        <span>
          {t("incomplete.title")}. {t("incomplete.body", { providers: names })}
        </span>
      </p>
    );
  }
  return (
    <p role="status" className={cn("flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-sm", className)}>
      <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-warning" />
      <span>
        <span className="font-medium">{t("incomplete.title")}.</span>{" "}
        <span className="text-muted-foreground">{t("incomplete.body", { providers: names })}</span>
      </span>
    </p>
  );
}
