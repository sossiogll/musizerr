import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, MoreHorizontal, ShieldCheck, ShieldOff, Trash2, UserCheck, UserPlus, UserX } from "lucide-react";
import { adminDeleteUser, adminListUsers, adminUpdateUser } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import { useAuth } from "@/contexts/AuthContext";
import { useCapabilities } from "@/hooks/useCapabilities";
import { useServerAction } from "@/hooks/useServerAction";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { CreateUserDialog } from "./CreateUserDialog";
import { UserDownloadPolicyPanel } from "./UserDownloadPolicy";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n";
import type { User, UserRole } from "@/types";

/**
 * The instance's accounts: one row each, with the actions in a menu (no
 * table to scroll sideways on a phone). Deleting asks first. Whether an
 * action is allowed is the server's decision; the own account's actions are
 * only greyed out as a hint.
 */
export function AdminUsersPage() {
  const { user: currentUser } = useAuth();
  const { mode } = useCapabilities();
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const [deleting, setDeleting] = useState<User | null>(null);
  const [creating, setCreating] = useState(false);

  const { data: users, isLoading, error } = useQuery({ queryKey: queryKeys.adminUsers, queryFn: adminListUsers });
  const onError = () => toast.error(t("admin.actionFailed"));
  const update = useMutation({
    mutationFn: ({ id, updates }: { id: string; updates: { role?: UserRole; is_active?: boolean } }) => adminUpdateUser(id, updates),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers }),
    onError,
  });
  const remove = useMutation({
    mutationFn: adminDeleteUser,
    onSuccess: () => {
      toast.success(t("admin.deleted"));
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers });
    },
    onError,
  });

  if (isLoading) {
    return (
      <div aria-hidden className="space-y-2">
        {Array.from({ length: 3 }).map((_, i) => (
          <Skeleton key={i} className="h-16 w-full rounded-lg" />
        ))}
      </div>
    );
  }
  if (error) return <ApiErrorMessage className="text-sm text-destructive" error={error} fallback={t("admin.loadFailed")} />;

  return (
    <section aria-labelledby="users-title" className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <h2 id="users-title" className="font-display text-lg font-semibold">
          {t("admin.usersCount", { count: users?.total ?? 0 })}
        </h2>
        <Button size="sm" className="h-11 sm:h-9" onClick={() => setCreating(true)}>
          <UserPlus aria-hidden />
          {t("admin.new.open")}
        </Button>
      </div>
      {mode === "open" && <p className="text-sm text-muted-foreground">{t("admin.openModeAccounts")}</p>}
      {users?.items.length === 0 && <p className="text-sm text-muted-foreground">{t("admin.noAccounts")}</p>}
      <ul className="divide-y rounded-lg border">
        {users?.items.map((u) => (
          <UserRow
            key={u.id}
            user={u}
            isSelf={u.id === currentUser?.id}
            busy={update.isPending || remove.isPending}
            onUpdate={(updates) => update.mutate({ id: u.id, updates })}
            onDelete={() => setDeleting(u)}
          />
        ))}
      </ul>
      <CreateUserDialog open={creating} onOpenChange={setCreating} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={t("admin.deleteTitle", { name: deleting?.username ?? "" })}
        description={t("admin.deleteBody")}
        confirmLabel={t("admin.delete")}
        destructive
        onConfirm={() => deleting && remove.mutate(deleting.id)}
      />
    </section>
  );
}

function UserRow({
  user,
  isSelf,
  busy,
  onUpdate,
  onDelete,
}: {
  user: User;
  isSelf: boolean;
  busy: boolean;
  onUpdate: (updates: { role?: UserRole; is_active?: boolean }) => void;
  onDelete: () => void;
}) {
  const { t } = useI18n();
  const server = useServerAction();
  const disabled = isSelf || busy || server.disabled;
  const [policyOpen, setPolicyOpen] = useState(false);
  return (
    <li>
      <div className="flex items-center gap-3 px-4 py-3">
        <span aria-hidden className="grid size-10 shrink-0 place-items-center rounded-full bg-primary/15 font-semibold uppercase text-primary">
          {user.username.slice(0, 1)}
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">
            {user.username}
            {isSelf && <span className="ml-1.5 text-xs font-normal text-muted-foreground">{t("admin.you")}</span>}
          </p>
          <p className="truncate text-sm text-muted-foreground">{user.email ?? "—"}</p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1 sm:flex-row sm:items-center sm:gap-2">
          <Badge variant={user.role === "admin" ? "default" : "secondary"}>{user.role === "admin" ? t("admin.roleAdmin") : t("admin.roleUser")}</Badge>
          {user.pending_approval ? (
            <Badge variant="outline">{t("admin.pendingApproval")}</Badge>
          ) : (
            !user.is_active && <Badge variant="destructive">{t("admin.inactive")}</Badge>
          )}
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="size-11 shrink-0" aria-label={t("admin.actionsFor", { name: user.username })} disabled={isSelf}>
              <MoreHorizontal aria-hidden />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-52">
            <DropdownMenuItem disabled={disabled} onSelect={() => onUpdate({ role: user.role === "admin" ? "user" : "admin" })}>
              {user.role === "admin" ? <ShieldOff aria-hidden className="size-4" /> : <ShieldCheck aria-hidden className="size-4" />}
              {user.role === "admin" ? t("admin.demote") : t("admin.promote")}
            </DropdownMenuItem>
            <DropdownMenuItem disabled={disabled} onSelect={() => onUpdate({ is_active: !user.is_active })}>
              {user.is_active ? <UserX aria-hidden className="size-4" /> : <UserCheck aria-hidden className="size-4" />}
              {user.is_active ? t("admin.deactivate") : user.pending_approval ? t("admin.approve") : t("settings.activate")}
            </DropdownMenuItem>
            {/* An admin is not held to the policy: only the accounts that download as users have one. */}
            {user.role !== "admin" && (
              <DropdownMenuItem onSelect={() => setPolicyOpen((open) => !open)}>
                <Download aria-hidden className="size-4" />
                {t(policyOpen ? "userPolicy.hide" : "userPolicy.show")}
              </DropdownMenuItem>
            )}
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled={disabled} className="text-destructive focus:text-destructive" onSelect={onDelete}>
              <Trash2 aria-hidden className="size-4" />
              {t("admin.delete")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {policyOpen && <UserDownloadPolicyPanel user={user} />}
    </li>
  );
}
