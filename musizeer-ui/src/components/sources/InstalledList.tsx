import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ArrowUpCircle, Trash2 } from "lucide-react";
import { uninstallProvider } from "@/lib/api-client";
import { absoluteTime } from "@/lib/dates";
import { useErrorText, useFailureToast } from "@/lib/error-text";
import { invalidateProviders, shortDigest } from "@/lib/sources";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { GatewayProviderState, InstalledProvider } from "@/types";
import { GatewayStateBadge } from "./GatewayStateBadge";
import { RoleBadges } from "./RoleBadges";

interface InstalledListProps {
  installed: InstalledProvider[];
  /** What the gateway says of each provider, by name (absent: it has not heard of it yet). */
  states: ReadonlyMap<string, GatewayProviderState>;
  /** Providers this Musizeer refused to register although the gateway runs them, by name. */
  hostRejected: Record<string, string>;
  /** Entries the gateway refused to take, by name. */
  gatewayRejected: Record<string, string>;
  onReviewUpdate: (installed: InstalledProvider) => void;
}

/**
 * What is installed, with the gateway's live state for each (running, downloading, refused, unavailable...
 * and why), whether its catalog offers an update, and uninstalling (which asks first).
 */
export function InstalledList({ installed, states, hostRejected, gatewayRejected, onReviewUpdate }: InstalledListProps) {
  const { t, locale } = useI18n();
  const errorText = useErrorText();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const failed = useFailureToast();
  const [uninstalling, setUninstalling] = useState<InstalledProvider | null>(null);

  const uninstall = useMutation({
    mutationFn: (name: string) => uninstallProvider(name),
    onSuccess: () => {
      toast.success(t("sources.uninstalled"));
      void invalidateProviders(queryClient);
    },
    onError: (error) => failed(error, t("sources.uninstallFailed")),
  });

  return (
    <>
      <ul className="space-y-2">
        {installed.map((provider) => {
          const state = states.get(provider.name);
          const problem = state && state.state !== "running" ? state.reason : "";
          return (
            <li key={provider.name}>
              <Card className="space-y-2 p-4">
                <div className="flex flex-wrap items-center gap-3">
                  <div className="min-w-0 flex-1 basis-56">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="text-sm font-medium">{provider.label}</p>
                      <RoleBadges roles={provider.roles} />
                      <GatewayStateBadge state={state?.state} />
                      {provider.update && <span className="text-xs font-medium text-info">{t("sources.updateAvailable")}</span>}
                    </div>
                    <p className="break-all font-mono text-xs text-muted-foreground">
                      {provider.image}@{shortDigest(provider.digest)}
                    </p>
                    <p className="text-xs text-muted-foreground">
                      {t("sources.from", { source: provider.source_name ?? "?" })} · {t("sources.approvedOn", { date: absoluteTime(provider.approved_at, locale) })}
                    </p>
                  </div>
                  <div className="flex items-center gap-1">
                    {provider.update && (
                      <Button
                        variant="outline"
                        size="sm"
                        aria-label={t("sources.reviewUpdate", { name: provider.label })}
                        disabled={server.disabled}
                        onClick={() => onReviewUpdate(provider)}
                      >
                        <ArrowUpCircle className="mr-1 h-4 w-4" />
                        {t("sources.updateAvailable")}
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="icon"
                      className="text-destructive"
                      aria-label={t("sources.uninstall", { name: provider.label })}
                      disabled={server.disabled}
                      onClick={() => setUninstalling(provider)}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
                {problem && <p className="text-xs text-destructive">{problem}</p>}
                {gatewayRejected[provider.name] && <p className="text-xs text-destructive">{t("sources.gatewayRefusedEntry", { name: provider.label, reason: gatewayRejected[provider.name] })}</p>}
                {hostRejected[provider.name] && <p className="text-xs text-warning">{t("sources.hostRejected", { reason: hostRejected[provider.name] })}</p>}
                {provider.update_error && provider.update_error !== "source_disabled" && (
                  <p className="text-xs text-muted-foreground">{t("sources.updateCheckFailed", { reason: errorText(provider.update_error, provider.update_error) })}</p>
                )}
              </Card>
            </li>
          );
        })}
      </ul>
      <ConfirmDialog
        open={uninstalling !== null}
        onOpenChange={(open) => !open && setUninstalling(null)}
        title={t("sources.uninstallTitle", { name: uninstalling?.label ?? "" })}
        description={t("sources.uninstallBody")}
        confirmLabel={t("sources.uninstallConfirm")}
        destructive
        onConfirm={() => uninstalling && uninstall.mutate(uninstalling.name)}
      />
    </>
  );
}
