import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/contexts/AuthContext";
import { useIsSignedIn } from "@/contexts/PreferencesSync";
import { useTheme, type Theme } from "@/contexts/ThemeContext";
import { useI18n, type Locale } from "@/i18n";
import { getSettingsSchema, revokeAllAuth } from "@/lib/api-client";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SettingsArea, SettingRow } from "./settings/SettingsArea";

/**
 * Sezione Account, ospitata dentro Settings. Il blocco identità (username,
 * ruolo, logout, danger zone) resta lo stesso di prima di Fase 5a — solo
 * quello, e SOLO quando esiste un vero account (`signedIn`, vedi
 * `useIsSignedIn` — single source of truth condivisa con PreferencesSync).
 * Lingua, tema e le altre preferenze personali (SettingDef scope="user" nel
 * gruppo "account" — vedi core/settings_catalog.py) vengono dalla stessa
 * dichiarazione via
 * `SettingsArea`, non più duplicate a mano in questo file — TRANNE in
 * modalità no-login, dove lingua/tema non hanno alcun account server-side a
 * cui essere attribuiti: `LocalPreferences` sotto li rende invece come
 * controlli puramente locali (ThemeContext/I18nContext), e `excludeScope`
 * toglie ogni impostazione scope="user" (owner-less qui) dalla vista
 * generica — decisione presa dallo `scope` dichiarato, mai da un elenco di
 * chiavi, cosi' una futura impostazione personale sparisce da sola in
 * questa modalità senza toccare questo file.
 */
export function AccountTab() {
  const { user, logout } = useAuth();
  const signedIn = useIsSignedIn();
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

  // Nessun account reale in modalità no-login (vedi index.tsx::isAreaVisible,
  // che per questo mostra comunque il tab): niente identità, niente logout,
  // niente danger zone — non c'è nulla da disconnettere o eliminare.
  if (!signedIn) {
    return (
      <div className="space-y-6">
        <LocalPreferences />
        <SettingsArea area="account" excludeScope="user" />
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

/**
 * Lingua e tema in modalità no-login: nessun account server-side possiede
 * questa scelta (vedi SettingsService._is_synthetic_user lato backend, che
 * per lo stesso motivo rifiuta di scrivere queste chiavi per l'utente
 * sintetico) — restano quindi puramente locali, cablate direttamente su
 * ThemeContext/I18nContext (`setTheme`/`setLocale`, mai `applyServer*`, che è
 * riservato alla riconciliazione da PreferencesSync) e mai su PATCH
 * /settings. Riusa `SettingRow` (lo stesso controllo generico guidato dal
 * `type` dichiarato in GET /settings/schema) solo per l'aspetto — label e
 * descrizione restano quelle dichiarate nel catalogo, mai duplicate qui.
 */
function LocalPreferences() {
  const { t, locale, setLocale } = useI18n();
  const { theme, setTheme } = useTheme();
  const { data: schemaData } = useQuery({ queryKey: ["settings-schema"], queryFn: getSettingsSchema });

  const languageDef = schemaData?.settings.find((s) => s.key === "language");
  const themeDef = schemaData?.settings.find((s) => s.key === "theme");

  if (!languageDef && !themeDef) return null;

  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">{t("account.localPreferences")}</h3>
      <p className="mb-3 text-xs text-muted-foreground">{t("account.localPreferencesHint")}</p>
      <Separator className="mb-3" />
      <div className="space-y-4">
        {languageDef && (
          <SettingRow
            def={languageDef}
            value={locale}
            onChange={(v) => setLocale(v as Locale)}
            providersData={undefined}
          />
        )}
        {themeDef && (
          <SettingRow
            def={themeDef}
            value={theme}
            onChange={(v) => setTheme(v as Theme)}
            providersData={undefined}
          />
        )}
      </div>
    </Card>
  );
}
