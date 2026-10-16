import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { getInstalledProviders, getProviderGatewayState, getSources } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { CatalogBrowser } from "@/components/sources/CatalogBrowser";
import { InstallDialog, type InstallTarget } from "@/components/sources/InstallDialog";
import { InstalledList } from "@/components/sources/InstalledList";
import { SourceDialog, type SourceTarget } from "@/components/sources/SourceDialog";
import { SourceList } from "@/components/sources/SourceList";
import { UpdateDialog, type UpdateTarget } from "@/components/sources/UpdateDialog";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { GatewayProviderState, ProviderGatewayState } from "@/types";

const GATEWAY_REFRESH_MS = 5000;

/**
 * Administration › Sources: where providers come from and what is installed.
 *
 * - Sources: the addresses of catalogs (a private one carries a token); enable, edit, remove.
 * - Installed providers: with the gateway's live state for each and the update its catalog offers.
 * - Catalogs: what each enabled source lists; installing shows the permissions first.
 *
 * Nothing here knows a provider by name: labels, roles and permissions all come from the catalogs.
 */
export function AdminSourcesPage() {
  const { t } = useI18n();
  const server = useServerAction();
  const [editing, setEditing] = useState<SourceTarget | null>(null);
  const [installing, setInstalling] = useState<InstallTarget | null>(null);
  const [updating, setUpdating] = useState<UpdateTarget | null>(null);

  const sources = useQuery({ queryKey: queryKeys.sources, queryFn: getSources });
  const installed = useQuery({ queryKey: queryKeys.installedProviders, queryFn: getInstalledProviders });
  const gateway = useQuery({ queryKey: queryKeys.providerGateway, queryFn: getProviderGatewayState, refetchInterval: GATEWAY_REFRESH_MS });
  const states = useMemo(() => new Map<string, GatewayProviderState>((gateway.data?.providers ?? []).map((p) => [p.name, p])), [gateway.data]);
  const enabledSources = (sources.data ?? []).filter((source) => source.enabled);

  return (
    <div className="space-y-8">
      <p className="text-sm text-muted-foreground">{t("sources.intro")}</p>

      <section aria-label={t("sources.sourcesHeading")} className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h2 className="font-display text-lg font-semibold">{t("sources.sourcesHeading")}</h2>
          <Button size="sm" disabled={server.disabled} title={server.reason} onClick={() => setEditing({ mode: "add" })}>
            <Plus className="mr-1 h-4 w-4" />
            {t("sources.addSource")}
          </Button>
        </div>
        {sources.isLoading && <Skeleton className="h-16 w-full" />}
        {sources.error && <ApiErrorMessage error={sources.error} fallback={t("sources.sourcesFailed")} className="text-sm text-destructive" />}
        {sources.data && sources.data.length === 0 && <p className="text-sm text-muted-foreground">{t("sources.noSources")}</p>}
        {sources.data && sources.data.length > 0 && (
          <SourceList sources={sources.data} installed={installed.data ?? []} onEdit={(source) => setEditing({ mode: "edit", source })} />
        )}
      </section>

      <section aria-label={t("sources.installedHeading")} className="space-y-3">
        <h2 className="font-display text-lg font-semibold">{t("sources.installedHeading")}</h2>
        <GatewayNotice state={gateway.data} />
        {installed.isLoading && <Skeleton className="h-16 w-full" />}
        {installed.error && <ApiErrorMessage error={installed.error} fallback={t("sources.installedFailed")} className="text-sm text-destructive" />}
        {installed.data && installed.data.length === 0 && <p className="text-sm text-muted-foreground">{t("sources.noInstalled")}</p>}
        {installed.data && installed.data.length > 0 && (
          <InstalledList
            installed={installed.data}
            states={states}
            hostRejected={gateway.data?.host_rejected ?? {}}
            gatewayRejected={gateway.data?.rejected ?? {}}
            onReviewUpdate={(provider) => provider.update && setUpdating({ installed: provider, offer: provider.update })}
          />
        )}
      </section>

      {enabledSources.length > 0 && (
        <section aria-label={t("sources.catalogHeading")} className="space-y-4">
          <h2 className="font-display text-lg font-semibold">{t("sources.catalogHeading")}</h2>
          {enabledSources.map((source) => (
            <CatalogBrowser key={source.id} source={source} onInstall={setInstalling} />
          ))}
        </section>
      )}

      <SourceDialog target={editing} onClose={() => setEditing(null)} />
      <InstallDialog target={installing} onClose={() => setInstalling(null)} />
      <UpdateDialog target={updating} onClose={() => setUpdating(null)} />
    </div>
  );
}

/** The one-line verdict on the gateway when it is not simply running: unreachable, or no sandbox here. */
function GatewayNotice({ state }: { state: ProviderGatewayState | undefined }) {
  const { t } = useI18n();
  if (!state || state.status === "ok" || state.status === "degraded") return null;
  const text =
    state.status === "unreachable"
      ? t("sources.gatewayUnreachable")
      : t("sources.gatewayNoSandbox", { reason: state.sandbox?.reason ?? state.detail });
  return (
    <p role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-2.5 text-sm">
      {text}
    </p>
  );
}
