import { useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle } from "lucide-react";
import { addSource, editSource } from "@/lib/api-client";
import { invalidateProviders } from "@/lib/sources";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Input } from "@/components/ui/input";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { ProviderSource } from "@/types";

/** `null` adds a source; a source edits its token and registry mode (its address cannot change). */
export type SourceTarget = { mode: "add" } | { mode: "edit"; source: ProviderSource };

/**
 * Add a source, or change one: the address of a catalog, an optional token for a private catalog (a
 * password field: it is never shown again, only whether one is set) and whether its images may come from a
 * registry over plain HTTP — with the warning that goes with it.
 */
export function SourceDialog({ target, onClose }: { target: SourceTarget | null; onClose: () => void }) {
  return (
    <AlertDialog open={target !== null} onOpenChange={(open) => !open && onClose()}>
      {target && (
        <AlertDialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
          <SourceForm target={target} onClose={onClose} />
        </AlertDialogContent>
      )}
    </AlertDialog>
  );
}

function SourceForm({ target, onClose }: { target: SourceTarget; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const ids = { url: useId(), token: useId(), insecure: useId() };
  const editing = target.mode === "edit" ? target.source : null;
  const [url, setUrl] = useState(editing?.url ?? "");
  const [token, setToken] = useState("");
  const [removeToken, setRemoveToken] = useState(false);
  const [insecure, setInsecure] = useState(editing?.allow_insecure_registry ?? false);

  const secureUrl = url.trim().toLowerCase().startsWith("https://");
  const hasToken = token.trim() !== "";
  const keepsToken = Boolean(editing?.has_credential) && !removeToken;
  const tokenAllowed = !insecure && (editing ? editing.url.toLowerCase().startsWith("https://") : true);
  const invalid = (!editing && url.trim() === "") || (hasToken && !secureUrl) || (hasToken && insecure) || (keepsToken && insecure);

  const save = useMutation({
    mutationFn: () => {
      if (!editing) {
        return addSource({
          url: url.trim(),
          ...(hasToken ? { credential: token.trim() } : {}),
          allow_insecure_registry: insecure,
        });
      }
      return editSource(editing.id, {
        allow_insecure_registry: insecure,
        ...(hasToken ? { credential: token.trim() } : removeToken ? { credential: null } : {}),
      });
    },
    onSuccess: () => {
      toast.success(t(editing ? "sources.dialog.saved" : "sources.dialog.added"));
      void invalidateProviders(queryClient);
      onClose();
    },
  });

  return (
    <>
      <AlertDialogTitle>{editing ? t("sources.dialog.editTitle", { name: editing.name }) : t("sources.dialog.addTitle")}</AlertDialogTitle>
      <AlertDialogDescription>{t("sources.dialog.description")}</AlertDialogDescription>
      <div className="space-y-4">
        <div className="space-y-1.5">
          <label htmlFor={ids.url} className="text-sm font-medium">
            {t("sources.dialog.url")}
          </label>
          <Input
            id={ids.url}
            type="url"
            inputMode="url"
            autoComplete="off"
            placeholder="https://example.org/catalog.json"
            value={url}
            readOnly={Boolean(editing)}
            onChange={(e) => setUrl(e.target.value)}
            className="h-11 sm:h-9"
          />
          {!editing && <p className="text-xs text-muted-foreground">{t("sources.dialog.urlHint")}</p>}
        </div>

        <div className="space-y-1.5">
          <label htmlFor={ids.token} className="text-sm font-medium">
            {t("sources.dialog.token")}
          </label>
          <Input
            id={ids.token}
            type="password"
            autoComplete="new-password"
            value={token}
            disabled={!tokenAllowed || removeToken}
            placeholder={keepsToken ? t("sources.dialog.tokenKeep") : undefined}
            onChange={(e) => setToken(e.target.value)}
            className="h-11 sm:h-9"
          />
          <p className="text-xs text-muted-foreground">{t("sources.dialog.tokenHint")}</p>
          {hasToken && !secureUrl && <p className="text-xs text-destructive">{t("sources.dialog.tokenNeedsHttps")}</p>}
          {editing?.has_credential && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={removeToken} onChange={(e) => setRemoveToken(e.target.checked)} />
              {t("sources.dialog.tokenRemove")}
            </label>
          )}
        </div>

        <div className="space-y-1.5">
          <label htmlFor={ids.insecure} className="flex items-center gap-2 text-sm font-medium">
            <input id={ids.insecure} type="checkbox" checked={insecure} disabled={hasToken || keepsToken} onChange={(e) => setInsecure(e.target.checked)} />
            {t("sources.dialog.insecure")}
          </label>
          <p className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 p-2.5 text-xs">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
            {t("sources.dialog.insecureWarning")}
          </p>
        </div>

        {save.error && (
          <ApiErrorMessage error={save.error} fallback={t(editing ? "sources.dialog.saveFailed" : "sources.dialog.addFailed")} className="text-sm text-destructive" />
        )}
      </div>
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
        <AlertDialogAction
          disabled={invalid || server.disabled || save.isPending}
          title={server.reason}
          onClick={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          {editing ? t("sources.dialog.save") : t("sources.dialog.add")}
        </AlertDialogAction>
      </div>
    </>
  );
}
