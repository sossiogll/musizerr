import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, useParams } from "@tanstack/react-router";
import { TabLink, TabNav } from "@/components/TabNav";
import { AccountTab } from "./AccountTab";
import { LinkedAccountsTab } from "./LinkedAccountsTab";
import { SettingsArea } from "./settings/SettingsArea";
import { getSettingsSchema } from "@/lib/api-client";
import { ShieldAlert } from "lucide-react";
import { useCapabilities } from "@/hooks/useCapabilities";
import { useSignInSearch } from "@/hooks/useSignInSearch";
import { useI18n } from "@/i18n";

function isUnauthorized(error: unknown): boolean {
  return (error as { response?: { status?: number } } | null)?.response?.status === 401;
}

/**
 * Fase 5a: i tab sono guidati da `taxonomy.areas` (GET /settings/schema),
 * nell'ordine dichiarato lì — mai un elenco di quattro aree cablato qui. Una
 * quinta area dichiarata più avanti nel core compare da sola, senza alcuna
 * modifica a questo file.
 *
 * "account" porta contenuto proprio oltre alle sole impostazioni dichiarate
 * (identità): non si nasconde mai in base al conteggio di `settings`.
 * "Linked accounts" (account propri dei provider, GET /auth/linked-accounts)
 * è una scheda a sé, per chi ha la capability `linked_accounts`; gli account
 * condivisi dell'istanza e le impostazioni dei provider sono in
 * Amministrazione › Providers. Ogni altra area si nasconde quando non ha nulla di leggibile
 * per questo utente — lo stesso meccanismo che nasconde "system" (e spesso
 * "library") a un utente non-admin, dato che ogni loro voce nel CATALOG è
 * scope="system": nessun controllo di ruolo scritto qui.
 *
 * Le aree di sistema (ADMIN_AREAS), i provider e i routine vivono in
 * Amministrazione.
 *
 * Ogni area è una rotta, /config/<area>, raggiungibile con un link diretto;
 * /config (o un'area che questo utente non vede) porta all'area di default.
 */
const ADMIN_AREAS: ReadonlySet<string> = new Set(["system", "providers"]);

const LINKED_ACCOUNTS = "linked-accounts";

export function ConfigPage() {
  const { section } = useParams({ strict: false });
  const { mode, can } = useCapabilities();
  const { t, tx } = useI18n();
  const signIn = useSignInSearch();
  const { data: schemaData, error } = useQuery({
    queryKey: ["settings-schema"],
    queryFn: getSettingsSchema,
    // Un visitatore anonimo su un deployment con auth abilitata riceve 401:
    // niente da riottenere ritentando, meglio arrivare in fretta allo stato
    // "accedi" sotto invece di restare a mostrare lo scheletro finché i
    // retry di default non si esauriscono.
    retry: (failureCount, err) => !isUnauthorized(err) && failureCount < 1,
  });

  if (!schemaData) {
    if (isUnauthorized(error)) {
      return (
        <div className="space-y-6">
          <h1 className="font-display text-2xl font-bold max-sm:sr-only">{t("settings.title")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("settings.signInRequired")}{" "}
            <Link to="/login" search={signIn} className="font-medium text-primary underline underline-offset-4">
              {t("nav.signIn")}
            </Link>
          </p>
        </div>
      );
    }
    return (
      <div className="space-y-6">
        <h1 className="font-display text-2xl font-bold max-sm:sr-only">{t("settings.title")}</h1>
        <div className="space-y-3 mt-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-12 rounded-lg bg-muted animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  const areaHasSettings = (key: string) => schemaData.settings.some((s) => s.area === key);

  const isAreaVisible = (key: string): boolean => {
    // Visible for a real account, AND in open mode (sign-in off) even
    // though there is no account there — AccountTab itself decides what to
    // render in that case (no identity block, language/theme as
    // device-local controls, owner-less scope="user" settings hidden).
    if (key === "account") return can.settings_personal || mode === "open";
    // System-wide settings live in Administration › System.
    if (ADMIN_AREAS.has(key)) return false;
    return areaHasSettings(key);
  };

  const visibleAreas = schemaData.taxonomy.areas.filter((a) => isAreaVisible(a.key));
  const defaultTab = visibleAreas.some((a) => a.key === "account") ? "account" : can.linked_accounts ? LINKED_ACCOUNTS : visibleAreas[0]?.key;

  const tabs = visibleAreas.map((a) => ({ key: a.key, label: tx(a.label_key, a.label) }));
  if (can.linked_accounts) {
    // Right after Account: the two are about the viewer.
    const afterAccount = tabs.findIndex((tab) => tab.key === "account") + 1;
    tabs.splice(afterAccount, 0, { key: LINKED_ACCOUNTS, label: t("linkedAccounts.tab") });
  }

  // /config, an unknown section or one this viewer cannot see: go to the default one.
  if (!tabs.some((tab) => tab.key === section)) {
    return defaultTab ? <Navigate to="/config/$section" params={{ section: defaultTab }} replace /> : null;
  }

  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-bold max-sm:sr-only">{t("settings.title")}</h1>

      {mode === "open" && (
        <div role="alert" className="flex items-start gap-3 rounded-md border border-warning/40 bg-warning/10 p-4">
          <ShieldAlert aria-hidden className="mt-0.5 size-5 shrink-0 text-warning" />
          <div className="space-y-1 text-sm">
            <p className="font-semibold">{t("settings.openModeTitle")}</p>
            <p className="text-muted-foreground">{t("settings.openModeBody")}</p>
          </div>
        </div>
      )}

      <TabNav label={t("settings.sections")}>
        {tabs.map((tab) => (
          <TabLink key={tab.key} to="/config/$section" params={{ section: tab.key }}>
            {tab.label}
          </TabLink>
        ))}
      </TabNav>

      {section === "account" ? <AccountTab /> : section === LINKED_ACCOUNTS ? <LinkedAccountsTab /> : <SettingsArea key={section} area={section!} />}
    </div>
  );
}
