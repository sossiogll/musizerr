import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { updateProvider } from "@/lib/api-client";
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
import type { InstalledProvider, ProviderUpdateOffer } from "@/types";
import { PermissionsDiffView } from "./PermissionsDiffView";
import { PermissionsList } from "./PermissionsList";

export interface UpdateTarget {
  installed: InstalledProvider;
  offer: ProviderUpdateOffer;
}

/**
 * An update is a new digest in the catalog that the admin approves. The dialog shows the difference with
 * what they approved before (added hosts, folders, limits, roles; "asks for more" when it does) and the
 * permissions as they will be after it.
 */
export function UpdateDialog({ target, onClose }: { target: UpdateTarget | null; onClose: () => void }) {
  return (
    <AlertDialog open={target !== null} onOpenChange={(open) => !open && onClose()}>
      {target && (
        <AlertDialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
          <UpdateForm target={target} onClose={onClose} />
        </AlertDialogContent>
      )}
    </AlertDialog>
  );
}

function UpdateForm({ target, onClose }: { target: UpdateTarget; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const { installed, offer } = target;
  const update = useMutation({
    mutationFn: () => updateProvider(installed.name, offer.digest),
    onSuccess: () => {
      toast.success(t("sources.updateDialog.done", { name: offer.label }));
      void invalidateProviders(queryClient);
      onClose();
    },
  });
  return (
    <>
      <AlertDialogTitle>{t("sources.updateDialog.title", { name: installed.label })}</AlertDialogTitle>
      <AlertDialogDescription>{t("sources.updateDialog.description")}</AlertDialogDescription>
      <div className="space-y-4">
        <p className="break-all font-mono text-xs">{t("sources.updateDialog.versions", { from: shortDigest(installed.digest), to: shortDigest(offer.digest) })}</p>
        {offer.compatible ? (
          <>
            <PermissionsDiffView diff={offer.diff} />
            <PermissionsList permissions={offer.permissions} />
          </>
        ) : (
          <p role="alert" className="text-sm text-destructive">
            {t("sources.updateDialog.incompatible", { version: offer.provider_api_version })}
          </p>
        )}
        {installed.sends_source_token && <p className="text-sm text-warning">{t("sources.installDialog.tokenSent", { registry: offer.registry })}</p>}
        {update.error && <ApiErrorMessage error={update.error} fallback={t("sources.updateDialog.failed")} className="text-sm text-destructive" />}
      </div>
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
        <AlertDialogAction
          disabled={server.disabled || update.isPending || !offer.compatible}
          title={server.reason}
          onClick={(event) => {
            event.preventDefault();
            update.mutate();
          }}
        >
          {t("sources.updateDialog.confirm")}
        </AlertDialogAction>
      </div>
    </>
  );
}
