import { useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { adminCreateUser } from "@/lib/api-client";
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
import { Select } from "@/components/ui/select";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { UserRole } from "@/types";

/**
 * Administration › Users › New user: an account made by an administrator, with authentication on or off
 * (POST /admin/users). The server judges the username (taken: 409, worded from its code) and the password;
 * the form only keeps a request from leaving that cannot succeed.
 */
export function CreateUserDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      {open && (
        <AlertDialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
          <CreateUserForm onClose={() => onOpenChange(false)} />
        </AlertDialogContent>
      )}
    </AlertDialog>
  );
}

function CreateUserForm({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const roleId = useId();
  const [account, setAccount] = useState(EMPTY_ACCOUNT);
  const [role, setRole] = useState<UserRole>("user");
  const problem = accountProblem(account);

  const create = useMutation({
    mutationFn: () =>
      adminCreateUser({
        username: account.username.trim(),
        password: account.password,
        role,
        ...(account.email.trim() ? { email: account.email.trim() } : {}),
      }),
    onSuccess: (created) => {
      toast.success(t("admin.new.created", { name: created.username }));
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers });
      onClose();
    },
  });

  return (
    <>
      <AlertDialogTitle>{t("admin.new.title")}</AlertDialogTitle>
      <AlertDialogDescription>{t("admin.new.description")}</AlertDialogDescription>
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (!problem && !create.isPending) create.mutate();
        }}
      >
        <NewAccountFields value={account} onChange={setAccount} />
        <div className="space-y-1.5">
          <label htmlFor={roleId} className="text-sm font-medium">
            {t("admin.new.role")}
          </label>
          <Select id={roleId} value={role} onChange={(e) => setRole(e.target.value as UserRole)} className="w-full">
            <option value="user">{t("admin.roleUser")}</option>
            <option value="admin">{t("admin.roleAdmin")}</option>
          </Select>
        </div>
        {create.error && <ApiErrorMessage error={create.error} fallback={t("admin.new.failed")} className="text-sm text-destructive" />}
      </form>
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
        <AlertDialogAction
          disabled={problem !== null || server.disabled || create.isPending}
          title={server.reason}
          onClick={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          {t("admin.new.create")}
        </AlertDialogAction>
      </div>
    </>
  );
}
