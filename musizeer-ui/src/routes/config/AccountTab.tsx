import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/contexts/AuthContext";
import { useI18n } from "@/i18n";
import { revokeAllAuth } from "@/lib/api-client";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SettingsArea } from "./settings/SettingsArea";

/**
 * Sezione Account, ospitata dentro Settings. Il blocco identita' (username,
 * ruolo, logout, danger zone) esiste SOLO quando c'e' un utente con un
 * account (`user`): con l'autenticazione disattivata il proprietario
 * dell'istanza non ha identita' da mostrare ne' una sessione da chiudere,
 * ma ha le stesse preferenze personali di tutti (lingua, tema, sfondo, le
 * altre impostazioni scope="user" del gruppo "account" — core/settings_catalog.py),
 * salvate lato server per l'utente di sistema. Vengono tutte dalla stessa
 * dichiarazione via `SettingsArea`; nessuna e' scritta a mano qui.
 */
export function AccountTab() {
  const { user, logout } = useAuth();
  const { t } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  // Dopo il logout lo stato utente si azzera, ma restando sulla pagina Account
  // non si vede alcun cambiamento: portiamo l'utente alla home (che mostrerà
  // il login se serve). Invalidiamo anche le cache che dipendono dall'utente.
  const handleLogout = async () => {
    await logout();
    navigate({ to: "/" });
  };

  const revokeAllMutation = useMutation({
    mutationFn: revokeAllAuth,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      toast.success(
        data.removed > 0
          ? t("account.removedAccounts", { count: data.removed })
          : t("account.noPersonalAccounts"),
      );
    },
    onError: () => toast.error(t("providers.saveFailed")),
  });

  const handleRevokeAll = () => {
    if (window.confirm(t("account.deleteConfirm"))) {
      revokeAllMutation.mutate();
    }
  };

  // Con l'autenticazione disattivata non c'e' nessun utente: niente identita',
  // niente logout, niente danger zone (non c'e' nulla da eliminare: il suo
  // account e' quello condiviso, che si gestisce da Amministrazione).
  if (!user) {
    return (
      <div className="space-y-6">
        <SettingsArea area="account" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {user?.username}
            <Badge variant={user?.role === "admin" ? "default" : "secondary"}>
              {user?.role === "admin" ? t("admin.roleAdmin") : t("admin.roleUser")}
            </Badge>
          </CardTitle>
          {user?.email && <CardDescription>{user.email}</CardDescription>}
        </CardHeader>
        <CardContent>
          {/* Signing out is not destructive: an outline button, not a red one. */}
          <Button variant="outline" onClick={handleLogout}>
            {t("nav.signOut")}
          </Button>
        </CardContent>
      </Card>

      <SettingsArea area="account" />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("account.dangerZone")}</CardTitle>
          <CardDescription>{t("account.dangerZoneHint")}</CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            variant="outline"
            className="text-destructive"
            onClick={handleRevokeAll}
            disabled={revokeAllMutation.isPending}
          >
            {revokeAllMutation.isPending && (
              <Loader2 className="w-4 h-4 mr-1.5 animate-spin" />
            )}
            {t("account.removeAllAccounts")}
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
