import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { getSourceCatalog } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { ProviderSource } from "@/types";
import type { InstallTarget } from "./InstallDialog";
import { RoleBadges } from "./RoleBadges";

/**
 * The catalog of one enabled source: each entry with what it provides, whether it can be installed here
 * (a provider built for another version of the provider API is listed but not installable), whether it is
 * installed. Installing opens the dialog that shows the permissions.
 */
export function CatalogBrowser({ source, onInstall }: { source: ProviderSource; onInstall: (target: InstallTarget) => void }) {
  const { t } = useI18n();
  const server = useServerAction();
  const { data, isLoading, error } = useQuery({ queryKey: queryKeys.sourceCatalog(source.id), queryFn: () => getSourceCatalog(source.id) });

  return (
    <section aria-label={t("sources.catalogOf", { name: source.name })} className="space-y-2">
      <h3 className="text-sm font-medium text-muted-foreground">{t("sources.catalogOf", { name: source.name })}</h3>
      {isLoading && <Skeleton className="h-16 w-full" />}
      {error && <ApiErrorMessage error={error} fallback={t("sources.catalogFailed")} className="text-sm text-destructive" />}
      {data && data.entries.length === 0 && <p className="text-sm text-muted-foreground">{t("sources.catalogEmpty")}</p>}
      {data && data.entries.length > 0 && (
        <ul className="space-y-2">
          {data.entries.map((entry) => (
            <li key={entry.name}>
              <Card className="flex flex-wrap items-start gap-3 p-4">
                <div className="min-w-0 flex-1 basis-56 space-y-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="text-sm font-medium">{entry.label}</p>
                    <RoleBadges roles={entry.roles} />
                    {entry.installed && <Badge variant="success">{t("sources.installedBadge")}</Badge>}
                    {entry.update && <Badge variant="info">{t("sources.updateAvailable")}</Badge>}
                  </div>
                  {entry.description && <p className="text-sm text-muted-foreground">{entry.description}</p>}
                  {!entry.compatible && (
                    <p role="note" className="text-xs text-destructive">
                      {t("sources.incompatible", { version: entry.provider_api_version })}
                    </p>
                  )}
                  {entry.homepage && (
                    <a href={entry.homepage} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-xs text-primary hover:underline">
                      {t("sources.website")}
                      <ExternalLink className="h-3 w-3" aria-hidden />
                    </a>
                  )}
                </div>
                {!entry.installed && entry.compatible && (
                  <Button size="sm" aria-label={t("sources.install", { name: entry.label })} disabled={server.disabled} title={server.reason} onClick={() => onInstall({ source, entry })}>
                    {t("sources.installButton")}
                  </Button>
                )}
              </Card>
            </li>
          ))}
        </ul>
      )}
      {data && data.rejected.length > 0 && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer select-none">{t("sources.rejectedEntries", { count: data.rejected.length })}</summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {data.rejected.map((item) => (
              <li key={item.name}>
                <span className="font-mono">{item.name}</span>: {item.reason}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
