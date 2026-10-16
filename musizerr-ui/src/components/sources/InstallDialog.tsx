import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { installProvider } from "@/lib/api-client";
import { invalidateProviders, shortDigest } from "@/lib/sources";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { CatalogEntry, ProviderSource } from "@/types";
import { PermissionsList } from "./PermissionsList";
import { RoleBadges } from "./RoleBadges";

export interface InstallTarget {
  source: ProviderSource;
  entry: CatalogEntry;
}

/**
 * Installing a provider: the admin sees what the sandbox will be allowed (hosts, settings-named hosts,
 * folders, limits) and which registry the image comes from — and the source's token goes to — before
 * confirming. What is recorded is the digest that is on screen: if the catalog offers another by then, the
 * server refuses.
 */
export function InstallDialog({ target, onClose }: { target: InstallTarget | null; onClose: () => void }) {
  return (
    <AlertDialog open={target !== null} onOpenChange={(open) => !open && onClose()}>
      {target && (
        <AlertDialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
          <InstallForm target={target} onClose={onClose} />
        </AlertDialogContent>
      )}
    </AlertDialog>
  );
}

function InstallForm({ target, onClose }: { target: InstallTarget; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const { source, entry } = target;
  const install = useMutation({
    mutationFn: () => installProvider(source.id, entry.name, entry.digest),
    onSuccess: () => {
      toast.success(t("sources.installDialog.done", { name: entry.label }));
      void invalidateProviders(queryClient);
      onClose();
    },
  });
  return (
    <>
      <AlertDialogTitle>{t("sources.installDialog.title", { name: entry.label })}</AlertDialogTitle>
      <AlertDialogDescription>{t("sources.installDialog.description")}</AlertDialogDescription>
      <div className="space-y-4">
        {entry.description && <p className="text-sm">{entry.description}</p>}
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-muted-foreground">{t("sources.provides")}</dt>
          <dd className="flex flex-wrap gap-1">
            <RoleBadges roles={entry.roles} />
          </dd>
          <dt className="text-muted-foreground">{t("sources.image")}</dt>
          <dd className="break-all font-mono text-xs">
            {entry.image}@{shortDigest(entry.digest)}
          </dd>
        </dl>
        <PermissionsList permissions={entry.permissions} />
        {source.has_credential && <p className="text-sm text-warning">{t("sources.installDialog.tokenSent", { registry: entry.registry })}</p>}
        {install.error && <ApiErrorMessage error={install.error} fallback={t("sources.installDialog.failed")} className="text-sm text-destructive" />}
      </div>
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
        <AlertDialogAction
          disabled={server.disabled || install.isPending}
          title={server.reason}
          onClick={(event) => {
            event.preventDefault(); // stays open until the server has said yes
            install.mutate();
          }}
        >
          {t("sources.installDialog.confirm")}
        </AlertDialogAction>
      </div>
    </>
  );
}
