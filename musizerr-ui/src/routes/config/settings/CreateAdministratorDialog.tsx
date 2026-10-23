import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { adminCreateUser, patchSettings } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { EMPTY_ACCOUNT, NewAccountFields, accountProblem } from "@/components/admin/NewAccountFields";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useAuth } from "@/contexts/AuthContext";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { User } from "@/types";

/**
 * The refusal that opens this dialog: the server will not turn authentication on while no administrator
 * can sign in (`no_admin_account`, the `guard_on` of the setting). The server decides when; this is only
 * what the page does about that refusal.
 */
export const NO_ADMIN_ACCOUNT = "no_admin_account";

/**
 * The guided step of turning authentication on with nobody to sign in: it creates the administrator,
 * saves the batch that was refused (the switch included, `adopt_into` naming the new account so the owner's
 * data becomes theirs), and signs that administrator in, so the owner is never left outside the app.
 * Each step runs once: after a failure the next attempt picks up where the last one stopped.
 */
export function CreateAdministratorDialog({
  updates,
  open,
  onClose,
}: {
  /** The settings that were refused, the switch among them. */
  updates: Record<string, unknown>;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <AlertDialog open={open} onOpenChange={(next) => !next && onClose()}>
      {open && (
        <AlertDialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
          <Guided updates={updates} onClose={onClose} />
        </AlertDialogContent>
      )}
    </AlertDialog>
  );
}

function Guided({ updates, onClose }: { updates: Record<string, unknown>; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const { login } = useAuth();
  const [account, setAccount] = useState(EMPTY_ACCOUNT);
  // What the steps already done used: a retry after a failure goes on with the same account, whatever
  // the fields say now.
  const created = useRef<{ user: User; password: string } | null>(null);
  const saved = useRef(false);
  const problem = accountProblem(account);

  const run = useMutation({
    mutationFn: async () => {
      if (!created.current) {
        const password = account.password;
        created.current = {
          password,
          user: await adminCreateUser({ username: account.username.trim(), password, role: "admin" }),
        };
      }
      const { user, password } = created.current;
      if (!saved.current) {
        await patchSettings(updates, { adoptInto: user.id });
        saved.current = true;
      }
      await login(user.username, password);
      return user;
    },
    onSuccess: (admin) => {
      toast.success(t("admin.guided.done", { name: admin.username }));
      // What was read in open mode is the owner's no more: ask again as the signed-in administrator.
      void queryClient.invalidateQueries({ queryKey: ["settings-values"] });
      void queryClient.invalidateQueries({ queryKey: ["settings-schema"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers });
      onClose();
    },
  });

  return (
    <>
      <AlertDialogTitle>{t("admin.guided.title")}</AlertDialogTitle>
      <AlertDialogDescription>{t("admin.guided.description")}</AlertDialogDescription>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!problem && !run.isPending) run.mutate();
        }}
      >
        <NewAccountFields value={account} onChange={setAccount} withEmail={false} />        {run.error && <ApiErrorMessage error={run.error} fallback={t("admin.guided.failed")} className="text-sm text-destructive" />}
      </form>
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel disabled={run.isPending}>{t("common.cancel")}</AlertDialogCancel>
        <AlertDialogAction
          disabled={problem !== null || server.disabled || run.isPending}
          title={server.reason}
          onClick={(event) => {
            event.preventDefault();
            run.mutate();
          }}
        >
          {run.isPending ? t("admin.guided.working") : t("admin.guided.submit")}
        </AlertDialogAction>
      </div>
    </>
  );
}
