import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Pencil, Trash2 } from "lucide-react";
import { editSource, removeSource } from "@/lib/api-client";
import { useFailureToast } from "@/lib/error-text";
import { invalidateProviders } from "@/lib/sources";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { InstalledProvider, ProviderSource } from "@/types";

interface SourceListProps {
  sources: ProviderSource[];
  installed: InstalledProvider[];
  onEdit: (source: ProviderSource) => void;
}

/**
 * The registered sources: enable or disable one, edit its token and registry mode, remove it (which
 * uninstalls what was installed from it, so it asks first and says which providers go).
 */
export function SourceList({ sources, installed, onEdit }: SourceListProps) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const failed = useFailureToast();
  const [removing, setRemoving] = useState<ProviderSource | null>(null);

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => editSource(id, { enabled }),
    onSuccess: () => invalidateProviders(queryClient),
    onError: (error) => failed(error, t("sources.dialog.saveFailed")),
  });
  const remove = useMutation({
    mutationFn: (id: string) => removeSource(id),
    onSuccess: () => {
      toast.success(t("sources.removed"));
      void invalidateProviders(queryClient);
    },
    onError: (error) => failed(error, t("sources.removeFailed")),
  });
  const goingWith = removing ? installed.filter((provider) => provider.source_id === removing.id).map((provider) => provider.label) : [];

  return (
    <>
      <ul className="space-y-2">
        {sources.map((source) => (
          <li key={source.id}>
            <Card className="flex flex-wrap items-center gap-3 p-4">
              <div className="min-w-0 flex-1 basis-56">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="truncate text-sm font-medium">{source.name}</p>
                  {source.has_credential && <Badge variant="info">{t("sources.tokenSet")}</Badge>}
                  {source.allow_insecure_registry && <Badge variant="warning">{t("sources.insecureRegistry")}</Badge>}
                  {!source.enabled && <Badge variant="secondary">{t("sources.disabled")}</Badge>}
                </div>
                <p className="truncate text-xs text-muted-foreground">{source.url}</p>
              </div>
              <div className="flex items-center gap-1">
                <Switch
                  aria-label={t("sources.toggle", { name: source.name })}
                  checked={source.enabled}
                  disabled={server.disabled || toggle.isPending}
                  onCheckedChange={(enabled) => toggle.mutate({ id: source.id, enabled })}
                />
                <Button variant="ghost" size="icon" aria-label={t("sources.edit", { name: source.name })} disabled={server.disabled} onClick={() => onEdit(source)}>
                  <Pencil className="h-4 w-4" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="text-destructive"
                  aria-label={t("sources.remove", { name: source.name })}
                  disabled={server.disabled}
                  onClick={() => setRemoving(source)}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </div>
            </Card>
          </li>
        ))}
      </ul>
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title={t("sources.removeTitle", { name: removing?.name ?? "" })}
        description={goingWith.length > 0 ? t("sources.removeBodyWith", { providers: goingWith.join(", ") }) : t("sources.removeBodyNone")}
        confirmLabel={t("sources.removeConfirm")}
        destructive
        onConfirm={() => removing && remove.mutate(removing.id)}
      />
    </>
  );
}
