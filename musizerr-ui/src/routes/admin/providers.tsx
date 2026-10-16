import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, ExternalLink, Info, Pencil, Loader2, CheckCircle2, Copy, Trash2, XCircle } from "lucide-react";
import {
  getProviders,
  getSettingsSchema,
  getSettingsValues,
  saveApiKey,
  saveOAuthClient,
  deleteOAuthClient,
  saveCredentials,
  revokeAuth,
  getOAuthAuthorizeUrl,
  patchSettings,
  testProviderConnection,
  deleteProviderAccount,
  type ConfigSchemaResponse,
  type SettingDef,
} from "@/lib/api-client";
import { useAuthReturn } from "@/hooks/useAuthReturn";
import { useI18n, type MessageKey } from "@/i18n";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Collapsible, CollapsibleTrigger, CollapsibleContent } from "@/components/ui/collapsible";
import { CredentialFieldsForm } from "@/components/accounts/CredentialFieldsForm";
import { TestButton } from "@/components/accounts/TestButton";
import type { ProviderInfo, ProvidersResponse } from "@/types";
import { SettingsArea, SettingRow } from "../config/settings/SettingsArea";
import { activeProviderRefBadges, providerSettingsSchema } from "../config/settings/settingsHelpers";

// Ordine di VISUALIZZAZIONE delle categorie (Fase 5c, Task 4): lo stesso
// ordine in cui le sezioni compaiono più sotto — serve SOLO a scegliere, fra
// le categorie in cui un provider genuinamente condiviso compare (vedi
// `also_registered_in`), quale mostra l'editor pieno. Le chiavi sono i ruoli
// pluggable che il core conosce (stesse di `ProvidersResponse`), mai un nome
// di provider concreto.
const CATEGORIES: { key: keyof ProvidersResponse; titleKey: MessageKey }[] = [
  { key: "metadata", titleKey: "providersTab.metadata" },
  { key: "download", titleKey: "providersTab.download" },
  { key: "lyrics", titleKey: "providersTab.lyrics" },
  { key: "identification", titleKey: "providersTab.identification" },
  { key: "storage", titleKey: "providersTab.storage" },
  { key: "sync", titleKey: "providersTab.sync" },
];

/**
 * Task 4 (fix di regressione) — la categoria "home" (in ordine di
 * visualizzazione) di un'entry di provider, SOLO quando il backend conferma
 * che è lo STESSO oggetto registrato altrove (`also_registered_in`, calcolato
 * per identità in `routes/config.py::list_providers`, mai per `name`). Il
 * `name` da solo non basta: due CLASSI DISTINTE nel registry possono
 * condividere un nome pur essendo provider indipendenti, ciascuno con la
 * propria config — dedurre l'identità dal nome lato frontend le tratterebbe
 * per errore come una sola (regressione osservata dal vivo su un provider
 * separato in metadata e download).
 *
 * Ritorna `null` quando QUESTA entry va renderizzata con l'editor pieno (non
 * è la copia successiva di un provider condiviso): o perché non è registrata
 * altrove, o perché la categoria corrente è già la prima in ordine di
 * visualizzazione fra quelle dichiarate da `also_registered_in`.
 */
export function homeCategoryFor(
  provider: Pick<ProviderInfo, "also_registered_in">,
  category: keyof ProvidersResponse,
): (typeof CATEGORIES)[number]["key"] | null {
  const others = provider.also_registered_in ?? [];
  if (others.length === 0) return null;
  const known = new Set<string>([category, ...others]);
  const first = CATEGORIES.find((c) => known.has(c.key))?.key ?? category;
  return first === category ? null : first;
}

/**
 * Administration › Providers: how the instance uses each provider — its
 * settings, the OAuth2 app, and the instance's own (shared) account where the
 * provider's declared model has one. A user's own account is theirs to connect
 * in Settings › Linked accounts, never here.
 *
 * Fase 5a: la selezione del provider attivo (storage/sync) e l'ordine dei
 * download provider sono dichiarati nel CATALOG come `provider_ref`/`list`
 * (gruppo "provider_selection", area "providers") e renderizzati dal renderer
 * generico (`<SettingsArea area="providers" />`). La card di ciascun provider
 * legge le proprie voci da GET /settings/schema (filtrate sul proprio
 * namespace "provider.<nome>.", vedi `providerSettingsSchema`) e i valori da
 * GET /settings/values, salvando con le stesse chiavi namespaced via PATCH
 * /settings: il server coerce/valida per tipo dichiarato, mai il browser.
 */
export function AdminProvidersPage() {
  const { t } = useI18n();
  useAuthReturn();
  const { data, isLoading } = useQuery({
    queryKey: ["providers"],
    queryFn: getProviders,
  });
  // Schema+valori delle impostazioni: servono sia per sapere QUALE
  // provider_ref referenzia ciascun provider (badge) sia per costruire
  // l'editor di impostazioni di ciascuna card — stessa query key di
  // SettingsArea (cache condivisa, un solo fetch).
  const { data: schemaData } = useQuery({ queryKey: ["settings-schema"], queryFn: getSettingsSchema });
  const { data: valuesData } = useQuery({ queryKey: ["settings-values"], queryFn: getSettingsValues });

  const badgesFor = (providerName: string, category: string): string[] =>
    activeProviderRefBadges(schemaData?.settings, valuesData, category, providerName);

  if (isLoading)
    return (
      <div className="space-y-3 mt-4">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-16 rounded-lg bg-muted animate-pulse" />
        ))}
      </div>
    );

  return (
    <div className="space-y-8 mt-4">
      <p className="text-sm text-muted-foreground">{t("providersTab.adminIntro")}</p>
      {CATEGORIES.map(({ key, titleKey }) => (
        <ProviderGroup
          key={key}
          category={key}
          titleKey={titleKey}
          providers={data?.[key] ?? []}
          badgesFor={badgesFor}
          schemaData={schemaData}
          valuesData={valuesData}
        />
      ))}

      <SettingsArea area="providers" />
    </div>
  );
}

export function ProviderGroup({
  titleKey,
  category,
  providers,
  badgesFor,
  schemaData,
  valuesData,
}: {
  titleKey: MessageKey;
  category: keyof ProvidersResponse;
  providers: ProviderInfo[];
  badgesFor: (providerName: string, category: string) => string[];
  schemaData?: ConfigSchemaResponse;
  valuesData?: Record<string, unknown>;
}) {
  const { t } = useI18n();
  return (
    <div>
      <h2 className="text-sm font-medium text-muted-foreground mb-3">
        {t("providersTab.groupHeading", { title: t(titleKey) })}
      </h2>
      <div className="space-y-3">
        {providers.map((p) => {
          const badges = badgesFor(p.name, category);
          // Task 4 (fix di regressione): l'identità viene dal backend
          // (`also_registered_in`, per OGGETTO), mai dedotta dal `name` qui —
          // due entry con lo stesso nome ma `also_registered_in` che non si
          // richiamano a vicenda sono provider DISTINTI, non un duplicato:
          // ciascuna mantiene il proprio editor pieno.
          const homeCategory = homeCategoryFor(p, category);
          if (homeCategory !== null) {
            // Questo provider ha già il suo editor pieno in una sezione
            // precedente (display order) — qui solo una card compatta, mai un
            // secondo editor sullo stesso config salvato.
            const homeTitleKey = CATEGORIES.find((c) => c.key === homeCategory)?.titleKey;
            const homeTitle = homeTitleKey ? t(homeTitleKey) : String(homeCategory);
            return <CompactProviderCard key={p.name} provider={p} badges={badges} configuredInTitle={homeTitle} />;
          }
          return (
            <ProviderCard
              key={p.name}
              provider={p}
              badges={badges}
              schemaData={schemaData}
              valuesData={valuesData}
            />
          );
        })}
      </div>
    </div>
  );
}

/** Task 4 — card compatta per un provider già editabile altrove: label,
 * pallino di stato e badge di ruolo attivo, senza un secondo editor sullo
 * stesso config salvato (che disallineerebbe silenziosamente le due copie). */
function CompactProviderCard({
  provider,
  badges,
  configuredInTitle,
}: {
  provider: ProviderInfo;
  badges: string[];
  configuredInTitle: string;
}) {
  const { t } = useI18n();
  return (
    <Card>
      <CardHeader className="p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <CardTitle className="text-sm">{provider.label}</CardTitle>
            <ProviderStatus usable={provider.usable} />
            {badges.map((b) => (
              <Badge key={b} variant="secondary">
                {b}
              </Badge>
            ))}
          </div>
        </div>
        <p className="text-xs text-muted-foreground pt-1">
          {t("providers.configuredIn", { section: configuredInTitle })}
        </p>
      </CardHeader>
    </Card>
  );
}

export function ProviderCard({
  provider,
  badges,
  schemaData,
  valuesData,
}: {
  provider: ProviderInfo;
  badges?: string[];
  schemaData?: ConfigSchemaResponse;
  valuesData?: Record<string, unknown>;
}) {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const shared = provider.shared;
  // The settings of an API key or a shared login are edited with the account itself.
  const inlineSettings = provider.auth_flow === "api_key" || (provider.auth_flow === "credentials" && shared !== null);
  const [apiKey, setApiKey] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  // "editing" delle credenziali sollevato qui così può essere resettato quando
  // il salvataggio va a buon fine (altrimenti il form resta aperto fino al reload).
  const [editingCred, setEditingCred] = useState(false);
  // Open from the start when there is something to do: the instance's account
  // or the OAuth2 app is not set up yet.
  const [open, setOpen] = useState(
    (shared !== null && !shared.connected && provider.auth_flow !== "none") ||
      (provider.auth_flow === "oauth2" && !provider.client_id),
  );
  const [saveError, setSaveError] = useState<string | null>(null);

  // Voci di GET /settings/schema che appartengono a QUESTO provider (chiave
  // "provider.<nome>.<short_key>").
  const settingsFields: SettingDef[] = providerSettingsSchema(schemaData?.settings, provider.name);
  // Modifiche non ancora salvate, chiave namespaced -> valore grezzo del
  // controllo. Un campo NON presente qui riflette semplicemente il valore
  // effettivo corrente (`valuesData`): un campo mid-edit resta quello
  // digitato, esattamente come SettingsArea.
  const [settingsEdits, setSettingsEdits] = useState<Record<string, unknown>>({});
  const settingValueOf = (key: string): unknown => (key in settingsEdits ? settingsEdits[key] : valuesData?.[key]);
  const setSettingValue = (key: string, v: unknown) => setSettingsEdits((e) => ({ ...e, [key]: v }));
  const clearSettingsEdits = () => setSettingsEdits({});

  const invalidateAfterSave = () => {
    queryClient.invalidateQueries({ queryKey: ["providers"] });
    queryClient.invalidateQueries({ queryKey: ["linked-accounts"] });
    queryClient.invalidateQueries({ queryKey: ["settings-values"] });
  };

  // Disconnects the instance's own account (the shared one) — never anybody's personal one.
  const revokeMutation = useMutation({
    mutationFn: () => revokeAuth(provider.name, "shared"),
    onSuccess: () => {
      invalidateAfterSave();
      toast.success(t("providersTab.accountRemoved", { label: provider.label }));
    },
  });

  // Save → test → rollback on failure. Config saved FIRST (e.g. URL for
  // an external server) via the generic PATCH /settings, so test_connection can use it.
  const apiKeyMutation = useMutation({
    mutationFn: async () => {
      setSaveError(null);
      if (Object.keys(settingsEdits).length > 0) {
        await patchSettings(settingsEdits);
      }
      await saveApiKey(provider.name, apiKey);
      const res = await testProviderConnection(provider.name, "account", "shared");
      const r = res.account;
      if (r && !r.ok) {
        await revokeAuth(provider.name, "shared");
        throw new Error(r.detail);
      }
    },
    onSuccess: () => {
      invalidateAfterSave();
      toast.success(t("providersTab.apiKeySaved"));
      setApiKey("");
      clearSettingsEdits();
    },
    onError: (e: Error) => {
      invalidateAfterSave();
      setSaveError(e.message);
    },
  });

  const clientMutation = useMutation({
    mutationFn: async () => {
      setSaveError(null);
      await saveOAuthClient(provider.name, clientId, clientSecret);
      const res = await testProviderConnection(provider.name, "app");
      const r = res.app;
      if (r && !r.ok) {
        await deleteOAuthClient(provider.name);
        throw new Error(r.detail);
      }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      queryClient.invalidateQueries({ queryKey: ["linked-accounts"] });
      toast.success(t("providersTab.clientCredentialsSaved"));
      setClientId("");
      setClientSecret("");
    },
    onError: (e: Error) => {
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      setSaveError(e.message);
    },
  });

  const deleteClientMutation = useMutation({
    mutationFn: () => deleteOAuthClient(provider.name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      queryClient.invalidateQueries({ queryKey: ["linked-accounts"] });
      toast.success(t("providersTab.appCredentialsRemoved"));
    },
  });

  const credentialsMutation = useMutation({
    mutationFn: async (values: Record<string, string>) => {
      setSaveError(null);
      if (Object.keys(settingsEdits).length > 0) {
        await patchSettings(settingsEdits);
      }
      await saveCredentials(provider.name, values, "shared");
      const res = await testProviderConnection(provider.name, "account", "shared");
      const r = res.account;
      if (r && !r.ok) {
        await revokeAuth(provider.name, "shared");
        throw new Error(r.detail);
      }
    },
    onSuccess: () => {
      invalidateAfterSave();
      toast.success(t("providersTab.credentialsSaved"));
      setEditingCred(false); // torna alla vista account salvato senza reload
      clearSettingsEdits();
    },
    onError: (e: Error) => {
      invalidateAfterSave();
      setSaveError(e.message);
    },
  });

  const settingsMutation = useMutation({
    mutationFn: () => patchSettings(settingsEdits),
    onSuccess: () => {
      invalidateAfterSave();
      toast.success(t("settings.saved"));
      clearSettingsEdits();
    },
  });

  const sectionTitle = (label: string) => (
    <div className="flex items-center gap-2 pt-1">
      <Separator className="flex-1" />
      <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider">{label}</span>
      <Separator className="flex-1" />
    </div>
  );

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <Card>
        <CardHeader className="p-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <CardTitle className="text-sm">{provider.label}</CardTitle>
              {/* `authenticated` alone lies for a provider with no login
                  (auth_flow "none") — it is always true even when its config
                  is incomplete or it isn't actually available. `usable`
                  combines that signal with `config_valid` (and, server-side,
                  with is_available() for identification providers). */}
              <ProviderStatus usable={provider.usable} />
              {/* Una per ogni impostazione provider_ref il cui valore
                  attuale è questo provider — etichettata con la LABEL
                  dichiarata di quella impostazione (mai un nome di ruolo o di
                  provider scritto qui), derivata da schema+valori in
                  AdminProvidersPage (vedi `activeProviderRefBadges`). */}
              {(badges ?? []).map((b) => (
                <Badge key={b} variant="secondary">
                  {b}
                </Badge>
              ))}
            </div>
            <CollapsibleTrigger asChild>
              <Button variant="ghost" size="sm">
                {open ? t("providersTab.hide") : t("providersTab.configure")}
              </Button>
            </CollapsibleTrigger>
          </div>
          <p className="pt-1 text-xs text-muted-foreground">{t(`providersTab.model.${provider.account_model}`)}</p>
        </CardHeader>
        <CollapsibleContent>
          <CardContent className="space-y-4 pt-0">
            {/* ---- OAuth2: the app (client) and the instance's account ---- */}
            {provider.auth_flow === "oauth2" && (
              <>
                <div className="space-y-3">
                  {sectionTitle(t("providersTab.appSection"))}
                  <OAuthSection
                    provider={provider}
                    clientId={clientId}
                    setClientId={setClientId}
                    clientSecret={clientSecret}
                    setClientSecret={setClientSecret}
                    onSave={() => clientMutation.mutate()}
                    onDelete={() => deleteClientMutation.mutate()}
                    saving={clientMutation.isPending}
                    error={saveError}
                  />
                  {provider.client_id && <TestButton provider={provider.name} scope="app" />}
                </div>

                {shared !== null && (
                  <div className="space-y-3">
                    {sectionTitle(t("providersTab.sharedAccount"))}
                    <p className="text-xs text-muted-foreground flex items-center gap-1.5">
                      <Info className="w-3.5 h-3.5 shrink-0" />
                      {t("providersTab.sharedAccountHint")}
                    </p>
                    <div className="flex gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={!provider.client_id}
                        onClick={async () => {
                          try {
                            window.location.href = await getOAuthAuthorizeUrl(provider.name, "shared");
                          } catch {
                            toast.error(t("providersTab.authorizationFailed"));
                          }
                        }}
                      >
                        <ExternalLink className="w-3.5 h-3.5 mr-1.5" />
                        {shared.connected ? t("providersTab.changeAccount") : t("providersTab.connectSharedAccount")}
                      </Button>
                      {shared.connected && (
                        <Button variant="outline" size="sm" className="text-destructive" onClick={() => revokeMutation.mutate()}>
                          {t("providersTab.removeAccount")}
                        </Button>
                      )}
                    </div>
                    {shared.connected && <TestButton provider={provider.name} target="shared" />}
                  </div>
                )}
              </>
            )}

            {/* ---- API key ---- */}
            {provider.auth_flow === "api_key" && (
              <div className="space-y-3">
                {sectionTitle(t("providersTab.sharedAccount"))}
                <ApiKeySection
                  provider={provider}
                  settingsFields={settingsFields}
                  valueOf={settingValueOf}
                  setFieldValue={setSettingValue}
                  apiKey={apiKey}
                  setApiKey={setApiKey}
                  onSave={() => apiKeyMutation.mutate()}
                  onSaveSettings={() => settingsMutation.mutate()}
                  onRevoke={() => revokeMutation.mutate()}
                  saving={apiKeyMutation.isPending}
                  error={saveError}
                  onClearError={() => setSaveError(null)}
                />
                {shared?.connected && <TestButton provider={provider.name} target="shared" />}
              </div>
            )}

            {/* ---- Login: the instance's own, when the model has one ---- */}
            {provider.auth_flow === "credentials" && shared !== null && (
              <div className="space-y-3">
                {sectionTitle(t("providersTab.sharedAccount"))}
                <p className="text-xs text-muted-foreground flex items-center gap-1.5">
                  <Info className="w-3.5 h-3.5 shrink-0" />
                  {t("providersTab.sharedAccountHint")}
                </p>
                <SharedLoginSection
                  provider={provider}
                  settingsFields={settingsFields}
                  valueOf={settingValueOf}
                  setFieldValue={setSettingValue}
                  onSaveCredentials={(values) => credentialsMutation.mutate(values)}
                  onSaveSettings={() => settingsMutation.mutate()}
                  saving={credentialsMutation.isPending}
                  error={saveError}
                  onClearError={() => setSaveError(null)}
                  editing={editingCred}
                  setEditing={setEditingCred}
                  actions={
                    shared.connected ? (
                      <div className="flex items-center gap-2">
                        <TestButton provider={provider.name} target="shared" />
                        <Button variant="outline" size="sm" className="text-destructive" onClick={() => revokeMutation.mutate()}>
                          {t("providersTab.removeAccount")}
                        </Button>
                      </div>
                    ) : undefined
                  }
                />
              </div>
            )}

            {/* ---- External utility ---- */}
            {provider.auth_flow === "external_utility" && <ExternalUtilitySection provider={provider} />}

            {/* ---- No-auth providers test ---- */}
            {provider.auth_flow === "none" && (
              <div className="pt-1">
                <TestButton provider={provider.name} />
              </div>
            )}

            {/* Each user's own account is theirs to connect. */}
            {provider.personal !== null && (
              <p className="text-xs text-muted-foreground flex items-center gap-1.5">
                <Info className="w-3.5 h-3.5 shrink-0" />
                {t("providersTab.personalHint")}
              </p>
            )}

            {/* ---- Settings section ---- */}
            {!inlineSettings && settingsFields.length > 0 && (
              <div className="space-y-3">
                {sectionTitle(t("settings.title"))}
                <div className="space-y-4">
                  {settingsFields.map((f) => (
                    <SettingRow
                      key={f.key}
                      def={f}
                      value={settingValueOf(f.key)}
                      onChange={(v) => setSettingValue(f.key, v)}
                      providersData={undefined}
                    />
                  ))}
                </div>
                <Button onClick={() => settingsMutation.mutate()}>
                  {t("providersTab.saveSettings")}
                </Button>
              </div>
            )}
          </CardContent>
        </CollapsibleContent>
      </Card>
    </Collapsible>
  );
}

function OAuthSection({
  provider,
  clientId,
  setClientId,
  clientSecret,
  setClientSecret,
  onSave,
  onDelete,
  saving,
  error,
}: {
  provider: ProviderInfo;
  clientId: string;
  setClientId: (v: string) => void;
  clientSecret: string;
  setClientSecret: (v: string) => void;
  onSave: () => void;
  onDelete: () => void;
  saving?: boolean;
  error?: string | null;
}) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  const hasSaved = !!provider.client_id;

  if (hasSaved && !editing) {
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <div className="flex-1 space-y-1">
            <p className="text-xs text-muted-foreground">{t("providersTab.clientId")}</p>
            <p className="text-sm font-mono truncate">{provider.client_id}</p>
          </div>
          <div className="flex-1 space-y-1">
            <p className="text-xs text-muted-foreground">{t("providersTab.clientSecret")}</p>
            <p className="text-sm font-mono text-muted-foreground">••••••••</p>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 shrink-0"
            onClick={() => setEditing(true)}
          >
            <Pencil className="w-3.5 h-3.5" />
          </Button>
        </div>
        <Button
          variant="outline"
          size="sm"
          className="text-destructive"
          onClick={onDelete}
        >
          {t("providersTab.deleteAppCredentials")}
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">
        {t("providersTab.enterClientIdSecret")}
      </p>
      <div className="flex gap-2">
        <Input
          placeholder={t("providersTab.clientId")}
          value={clientId || (editing ? provider.client_id ?? "" : "")}
          onChange={(e) => setClientId(e.target.value)}
          className="flex-1"
        />
        <Input
          type="password"
          placeholder={t("providersTab.clientSecret")}
          value={clientSecret}
          onChange={(e) => setClientSecret(e.target.value)}
          className="flex-1"
        />
        <Button
          variant="secondary"
          onClick={() => onSave()}
          disabled={!(clientId || provider.client_id) || !clientSecret || saving}
        >
          {saving && <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />}
          {t("common.save")}
        </Button>
        {editing && (
          <Button variant="ghost" onClick={() => setEditing(false)}>
            {t("providersTab.cancel")}
          </Button>
        )}
      </div>
      {error && (
        <p className="text-xs text-destructive flex items-center gap-1">
          <XCircle className="w-3.5 h-3.5 shrink-0" />
          {error}
        </p>
      )}
    </div>
  );
}

function SharedLoginSection({
  provider,
  settingsFields,
  valueOf,
  setFieldValue,
  onSaveCredentials,
  onSaveSettings,
  saving,
  error,
  onClearError,
  editing,
  setEditing,
  actions,
}: {
  provider: ProviderInfo;
  settingsFields: SettingDef[];
  valueOf: (key: string) => unknown;
  setFieldValue: (key: string, v: unknown) => void;
  onSaveCredentials: (values: Record<string, string>) => void;
  onSaveSettings: () => void;
  saving?: boolean;
  error?: string | null;
  onClearError?: () => void;
  editing: boolean;
  setEditing: (v: boolean) => void;
  actions?: React.ReactNode;
}) {
  const { t } = useI18n();
  const fields = provider.credential_fields ?? [];
  const settings = settingsFields.length > 0 && (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <Separator className="flex-1" />
        <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider">{t("settings.title")}</span>
        <Separator className="flex-1" />
      </div>
      {settingsFields.map((f) => (
        <SettingRow key={f.key} def={f} value={valueOf(f.key)} onChange={(v) => { setFieldValue(f.key, v); onClearError?.(); }} providersData={undefined} />
      ))}
    </div>
  );

  // The instance's account is connected: who it is (a secret is never shown), and the settings below.
  if (provider.shared?.connected && !editing) {
    return (
      <div className="space-y-3">
        <div className="flex items-start gap-2">
          <div className="flex flex-1 flex-wrap gap-4">
            {fields.map((f) => (
              <div key={f.key} className="space-y-1">
                <p className="text-xs text-muted-foreground">{f.label}</p>
                <p className={f.secret ? "text-sm font-mono text-muted-foreground" : "text-sm font-mono"}>
                  {f.secret ? "••••••••" : f.key === "username" ? provider.shared?.username ?? "—" : "—"}
                </p>
              </div>
            ))}
          </div>
          <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" onClick={() => setEditing(true)}>
            <Pencil className="w-3.5 h-3.5" />
          </Button>
        </div>
        {actions}
        {settings}
        {settingsFields.length > 0 && <Button onClick={onSaveSettings}>{t("providersTab.saveSettings")}</Button>}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {settings}
      <CredentialFieldsForm
        fields={fields}
        saving={saving}
        error={error}
        onSubmit={onSaveCredentials}
        onCancel={editing ? () => { setEditing(false); onClearError?.(); } : undefined}
        onEdit={onClearError}
      />
    </div>
  );
}

// ------------------------------------------------------------------
// API key section
// ------------------------------------------------------------------

function ApiKeySection({
  provider,
  settingsFields,
  valueOf,
  setFieldValue,
  apiKey,
  setApiKey,
  onSave,
  onSaveSettings,
  onRevoke,
  saving,
  error,
  onClearError,
}: {
  provider: ProviderInfo;
  settingsFields: SettingDef[];
  valueOf: (key: string) => unknown;
  setFieldValue: (key: string, v: unknown) => void;
  apiKey: string;
  setApiKey: (v: string) => void;
  onSave: () => void;
  onSaveSettings: () => void;
  onRevoke: () => void;
  saving?: boolean;
  error?: string | null;
  onClearError?: () => void;
}) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);

  if (provider.shared?.connected && !editing) {
    return (
      <div className="space-y-2">
        <div className="flex items-start gap-2">
          <div className="flex-1 space-y-2">
            {settingsFields.map((f) => (
              <SettingRow key={f.key} def={f} value={valueOf(f.key)} onChange={(v) => setFieldValue(f.key, v)} providersData={undefined} />
            ))}
            <div className="space-y-1">
              <p className="text-xs text-muted-foreground">{t("providersTab.apiKey")}</p>
              <p className="text-sm font-mono text-muted-foreground">••••••••</p>
            </div>
          </div>
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 shrink-0"
            onClick={() => setEditing(true)}
          >
            <Pencil className="w-3.5 h-3.5" />
          </Button>
        </div>
        {settingsFields.length > 0 && (
          <Button variant="secondary" size="sm" onClick={onSaveSettings}>
            {t("providersTab.saveSettings")}
          </Button>
        )}
        <Button
          variant="outline"
          size="sm"
          className="text-destructive"
          onClick={onRevoke}
        >
          {t("providersTab.removeAccount")}
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {settingsFields.map((f) => (
        <SettingRow
          key={f.key}
          def={f}
          value={valueOf(f.key)}
          onChange={(v) => { setFieldValue(f.key, v); onClearError?.(); }}
          providersData={undefined}
        />
      ))}
      <div className="flex gap-2 pt-1">
        <Input
          type="password"
          placeholder={t("providersTab.apiKey")}
          value={apiKey}
          onChange={(e) => { setApiKey(e.target.value); onClearError?.(); }}
          className="flex-1"
        />
        <Button
          variant="secondary"
          onClick={() => onSave()}
          disabled={!apiKey || saving}
        >
          {saving && <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" />}
          {t("common.save")}
        </Button>
        {editing && (
          <Button variant="ghost" onClick={() => { setEditing(false); onClearError?.(); }}>
            {t("providersTab.cancel")}
          </Button>
        )}
      </div>
      {error && (
        <p className="text-xs text-destructive flex items-center gap-1">
          <XCircle className="w-3.5 h-3.5 shrink-0" />
          {error}
        </p>
      )}
    </div>
  );
}

// ------------------------------------------------------------------
// External utility section (auth driven by a third-party container)
// ------------------------------------------------------------------

function ExternalUtilitySection({ provider }: { provider: ProviderInfo }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const available = provider.library_available !== false;
  const accounts = provider.accounts ?? [];
  const displayFields = provider.account_display_fields ?? [];
  const borrowedFrom = provider.borrowed_oauth_client_from;
  const borrowedOk = provider.borrowed_oauth_client_ok !== false;

  const deleteMutation = useMutation({
    mutationFn: (name: string) => deleteProviderAccount(provider.name, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["providers"] });
      toast.success(t("providersTab.accountRemovedGeneric"));
    },
    onError: () => {
      toast.error(t("providersTab.removeAccountFailed"));
    },
  });

  const origin = typeof window !== "undefined" ? window.location.origin : "http://localhost";

  return (
    <div className={available ? "space-y-4" : "space-y-4 opacity-60 pointer-events-none"}>
      <div className="flex items-center gap-2 pt-1">
        <Separator className="flex-1" />
        <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wider">{t("providersTab.integration", { label: provider.label })}</span>
        <Separator className="flex-1" />
      </div>

      {/* ---- Library availability gate ---- */}
      {!available && provider.library_package && (
        <div className="rounded-sm border border-dashed border-muted-foreground/30 bg-muted/30 p-3 space-y-2 pointer-events-auto">
          <div className="flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />
            <div className="space-y-1 text-xs">
              <p className="font-medium">{t("providersTab.libraryNotInstalled", { label: provider.label })}</p>
              <p className="text-muted-foreground">
                {t("providersTab.installOptionalPrefix")} <code className="px-1 py-0.5 rounded-xs bg-muted">{provider.library_package}</code> {t("providersTab.installOptionalSuffix")}
              </p>
              {provider.library_url && (
                <p className="text-muted-foreground">
                  {t("providersTab.upstream")}{" "}
                  <a
                    href={provider.library_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="underline"
                  >
                    {provider.library_url.replace(/^https?:\/\//, "")}
                  </a>
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      {/* ---- Legal disclaimer ---- */}
      {provider.disclaimer && (
        <div className="flex items-start gap-2 text-[11px] text-muted-foreground">
          <AlertTriangle className="w-3.5 h-3.5 text-amber-500 shrink-0 mt-0.5" />
          <p>{provider.disclaimer}</p>
        </div>
      )}

      {/* ---- Prereq: OAuth client borrowed from another provider ---- */}
      {borrowedFrom && (
        <div className="space-y-1">
          <p className="text-xs font-medium">
            {t("providersTab.apiCredentialsFor", { provider: borrowedFrom.charAt(0).toUpperCase() + borrowedFrom.slice(1) })}
          </p>
          {borrowedOk ? (
            <p className="text-xs text-green-500 flex items-center gap-1">
              <CheckCircle2 className="w-3.5 h-3.5" />
              {t("providersTab.reusingCredentials", { provider: borrowedFrom })}
            </p>
          ) : (
            <p className="text-xs text-destructive flex items-center gap-1">
              <XCircle className="w-3.5 h-3.5" />
              {t("providersTab.configureClientFirst", { provider: borrowedFrom })}
            </p>
          )}
        </div>
      )}

      {/* ---- Registered accounts ---- */}
      <div className="space-y-2">
        <p className="text-xs font-medium">{t("providersTab.accounts")}</p>
        {accounts.length === 0 ? (
          <p className="text-xs text-muted-foreground">{t("providersTab.noAccountsRegistered")}</p>
        ) : (
          <div className="space-y-1.5">
            {accounts.map((a) => (
              <div key={a.name} className="flex items-center gap-2 rounded-sm border border-border bg-card/40 px-2.5 py-1.5">
                <span className="text-sm font-medium flex-1 truncate">{a.name}</span>
                {displayFields.map((f) => (
                  <span key={f.key} className="text-[10px] uppercase tracking-wider text-muted-foreground">{a[f.key]}</span>
                ))}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7 text-destructive"
                  onClick={() => deleteMutation.mutate(a.name)}
                  disabled={deleteMutation.isPending}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ---- Add account instructions ---- */}
      {provider.setup_command && (
        <div className="space-y-2">
          <p className="text-xs font-medium">{t("providersTab.registerNewAccount")}</p>
          {provider.setup_instructions && (
            <p className="text-xs text-muted-foreground">
              {provider.setup_instructions.replace(/\{origin\}/g, origin)}
            </p>
          )}
          <div className="rounded-sm bg-muted/50 p-2 font-mono text-[11px] flex items-start gap-2">
            <code className="flex-1 break-all">{provider.setup_command}</code>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6 shrink-0"
              onClick={() => {
                navigator.clipboard?.writeText(provider.setup_command!);
                toast.success(t("providersTab.commandCopied"));
              }}
            >
              <Copy className="w-3 h-3" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}


/** Whether a provider can be used right now, in words (not a coloured dot alone). */
function ProviderStatus({ usable }: { usable: boolean | undefined }) {
  const { t } = useI18n();
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs ${usable ? "text-success" : "text-muted-foreground"}`}>
      <span aria-hidden className={`size-2 rounded-full ${usable ? "bg-success" : "bg-muted-foreground"}`} />
      {usable ? t("providersTab.connected") : t("providersTab.notConnected")}
    </span>
  );
}
