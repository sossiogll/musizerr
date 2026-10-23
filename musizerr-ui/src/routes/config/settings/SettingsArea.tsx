import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Info } from "lucide-react";
import {
  getSettingsSchema,
  getSettingsValues,
  getProviders,
  patchSettings,
  startFullSyncVerify,
  getFullSyncVerifyStatus,
  type SettingDef,
} from "@/lib/api-client";
import type { ProviderInfo, ProvidersResponse } from "@/types";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { useI18n } from "@/i18n";
import type { ApiErrorBody } from "@/lib/api-errors";
import { useErrorText } from "@/lib/error-text";
import { queryKeys } from "@/lib/query-keys";
import { cn } from "@/lib/utils";
import { CreateAdministratorDialog, NO_ADMIN_ACCOUNT } from "./CreateAdministratorDialog";
import { SettingControl } from "./SettingControl";
import { ProviderOrderList } from "./ProviderOrderList";
import { ProviderRefControl } from "./ProviderRefControl";
import { RegisterWebhook } from "./RegisterWebhook";
import {
  findProviderByName,
  isLockedUnavailable,
  isProviderNamespacedKey,
  isSettingVisible,
  mergeProviderOrder,
  providersForCategory,
  settingDescription,
  settingLabel,
} from "./settingsHelpers";

interface SettingsAreaProps {
  /** Chiave di area dichiarata in `taxonomy.areas` (GET /settings/schema) —
   * es. "account" | "providers" | "library" | "system", o una futura area
   * dichiarata più avanti: questo componente non ne conosce l'elenco. */
  area: string;
}

/**
 * Fase 5a: renderizza UN'area della tassonomia impostazioni — i suoi gruppi
 * come sezioni nell'ordine dichiarato, ciascuno con label/descrizione da
 * `taxonomy.groups` (o dalla label del provider stesso per un gruppo che è
 * un provider), e ogni impostazione col controllo scelto dal suo `type`.
 * Sostituisce GeneralTab.tsx (ad hoc) + AdvancedTab.tsx (schema-driven ma
 * duplicato) con un solo renderer generico, guidato interamente da
 * GET /settings/schema + GET /settings/values.
 */
export function SettingsArea({ area }: SettingsAreaProps) {
  const { t, tx } = useI18n();
  const errorText = useErrorText();
  const queryClient = useQueryClient();
  const { data: schemaData } = useQuery({ queryKey: ["settings-schema"], queryFn: getSettingsSchema });
  const { data: values } = useQuery({ queryKey: ["settings-values"], queryFn: getSettingsValues });
  const { data: providersData } = useQuery({ queryKey: ["providers"], queryFn: getProviders });
  const [edits, setEdits] = useState<Record<string, unknown>>({});
  // The batch the server refused for want of an administrator: the guided step takes it from here.
  const [needsAdministrator, setNeedsAdministrator] = useState<Record<string, unknown> | null>(null);

  const mutation = useMutation({
    mutationFn: (updates: Record<string, unknown>) => patchSettings(updates),
    onSuccess: (data) => {
      // Stesso query key letto da PreferencesSync: un salvataggio da qui
      // (es. tema/lingua nell'area account) si riflette subito, senza
      // ricaricare la pagina.
      queryClient.setQueryData(["settings-values"], data);
      setEdits({});
      toast.success(t("settings.saved"));
      // A setting can change who the viewer is or what they may do (authentication on or off,
      // the download policy): the status is asked again.
      void queryClient.invalidateQueries({ queryKey: queryKeys.viewer });
    },
    onError: (err: Error & { response?: { data?: { detail?: string | ApiErrorBody } } }, updates) => {
      // Il batch fallisce TUTTO insieme: mostriamo esattamente il dettaglio
      // del backend (quale chiave, quale motivo) invece di un messaggio
      // generico — l'utente deve poter capire cosa correggere. Un rifiuto con
      // codice stabile (es. auth_secret_insecure) arriva tradotto.
      const detail = err.response?.data?.detail;
      if (detail && typeof detail === "object" && detail.error_code === NO_ADMIN_ACCOUNT) {
        setNeedsAdministrator(updates);
      } else if (detail && typeof detail === "object") {
        toast.error(errorText(detail.error_code, detail.error_message) || t("settings.saveFailed"));
      } else {
        toast.error(detail ?? t("settings.saveFailed"));
      }
    },
  });

  const valueOf = (key: string): unknown => (key in edits ? edits[key] : values?.[key]);
  const setValue = (key: string, v: unknown) => setEdits((e) => ({ ...e, [key]: v }));
  const dirtyKeys = Object.keys(edits);

  // Fase 5b: le impostazioni di provider (chiave "provider.<nome>.<key>")
  // hanno già la loro card in the admin providers page — un editor migliore, costruito
  // apposta per il flusso connetti-e-configura. Renderizzarle ANCHE qui
  // duplicava l'editor e disallineava le due copie (Fase 5b, difetto 1).
  // Riconosciute dalla FORMA della chiave, mai da un elenco di provider.
  const settingsForArea = useMemo(
    () =>
      (schemaData?.settings ?? []).filter(
        (s) =>
          s.area === area &&
          !isProviderNamespacedKey(s.key) &&
          !s.hidden,
      ),
    [schemaData, area],
  );

  // Ordine di visualizzazione dei gruppi: il primo incontro nell'elenco
  // (già nell'ordine stabile di merged_schema — CATALOG poi provider) fissa
  // la posizione. Nessuna mappa hardcoded: un gruppo core sconosciuto a
  // priori (o un futuro gruppo di provider) finisce comunque al posto giusto.
  const groupOrder = useMemo(() => {
    const order: string[] = [];
    for (const s of settingsForArea) if (!order.includes(s.group)) order.push(s.group);
    return order;
  }, [settingsForArea]);

  if (!schemaData || !values) {
    return (
      <div className="space-y-3 mt-4">
        {Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-16 rounded-lg bg-muted animate-pulse" />
        ))}
      </div>
    );
  }

  // Un'area senza nulla di leggibile per questo utente (es. "system" per un
  // non-admin, dato che ogni sua impostazione è scope="system") non
  // renderizza nulla — è così che il tab che la ospita sa di doversi
  // nascondere, senza alcun controllo di ruolo scritto qui o nel tab stesso.
  if (settingsForArea.length === 0) return null;

  const groupMeta = (groupKey: string): { label: string; description: string } => {
    const g = schemaData.taxonomy.groups.find((g) => g.key === groupKey);
    if (g) return { label: tx(g.label_key, g.label), description: tx(g.description_key, g.description) };
    const provider = findProviderByName(providersData, groupKey);
    return { label: provider?.label ?? groupKey, description: "" };
  };

  return (
    <div className="space-y-6 mt-4">
      {dirtyKeys.length > 0 && (
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 rounded-lg border bg-background/90 px-4 py-2 backdrop-blur">
          <span className="text-sm text-muted-foreground">
            {t("settings.unsavedChanges", { count: dirtyKeys.length })}
          </span>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={() => setEdits({})}>
              {t("settings.discard")}
            </Button>
            <Button size="sm" disabled={mutation.isPending} onClick={() => mutation.mutate(edits)}>
              {t("common.save")}
            </Button>
          </div>
        </div>
      )}

      {groupOrder.map((groupKey) => {
        // `visible_when` è valutato contro lo stato A SCHERMO (edits inclusi
        // via valueOf), non solo l'ultimo valore salvato: un toggle rivela
        // subito le sue impostazioni dipendenti, prima di qualunque salvataggio.
        const defs = settingsForArea.filter((s) => s.group === groupKey && isSettingVisible(s, valueOf));
        if (defs.length === 0) return null;
        const meta = groupMeta(groupKey);
        return (
          <Card key={groupKey} className="p-4">
            <h3 className="mb-1 text-sm font-semibold">{meta.label}</h3>
            {meta.description && (
              <p className="mb-3 text-xs text-muted-foreground">{meta.description}</p>
            )}
            <Separator className="mb-3" />
            <div className="space-y-4">
              {defs.map((def) => (
                // A setting that depends on another (`visible_when`) sits under it, indented:
                // the mode of a download request and, only for "up to", its threshold.
                <div key={def.key} className={cn(def.visible_when?.length ? "ml-1 border-l-2 pl-4" : undefined)}>
                  <SettingRow
                    def={def}
                    value={valueOf(def.key)}
                    onChange={(v) => setValue(def.key, v)}
                    providersData={providersData}
                  />
                </div>
              ))}
              {/* Fase < 5a (AdvancedTab.tsx): due widget d'azione keyati sul
                  nome del gruppo a cui appartengono concettualmente, non su
                  una singola chiave — spostati qui invariati. */}
              {groupKey === "library_sync" && <FullSyncVerify />}
              {groupKey === "reconciliation" && <RegisterWebhook />}
            </div>
          </Card>
        );
      })}
      <CreateAdministratorDialog
        open={needsAdministrator !== null}
        updates={needsAdministrator ?? {}}
        onClose={() => setNeedsAdministrator(null)}
      />
    </div>
  );
}

/**
 * Esportato oltre l'uso interno di questo file: AccountTab lo riusa per
 * lingua/tema in modalità no-login, cablato a `setTheme`/`setLocale` invece
 * che a `patchSettings` — stesso controllo generico guidato dal `type`
 * dichiarato, mai una seconda UI bespoke costruita a mano per quel caso.
 */
export function SettingRow({
  def,
  value,
  onChange,
  providersData,
}: {
  def: SettingDef;
  value: unknown;
  onChange: (v: unknown) => void;
  providersData: ProvidersResponse | undefined;
}) {
  // Fase 5c, Task 1: mostrato per QUALUNQUE tipo — il verdetto del server
  // (`available`/`unavailable_reason`) non distingue per tipo, quindi nemmeno
  // la sua presentazione lo fa. Il testo è già leggibile così com'è (costruito
  // dal backend da categoria/label dichiarate, mai un nome di provider).
  const reason = def.available === false ? def.unavailable_reason : null;
  const { tx } = useI18n();
  const label = settingLabel(def, tx);
  const description = settingDescription(def, tx);

  // Fase 5c, Task 2: un `provider_ref` non passa mai dalla barra di
  // salvataggio in batch — attivarlo è un'azione esplicita e testata a sé
  // (vedi ProviderRefControl). `value` qui resta il valore EFFETTIVO dal
  // server: questa chiave non entra mai nello stato `edits` di SettingsArea.
  if (def.type === "provider_ref") {
    return (
      <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 sm:pr-4">
          <div className="text-sm font-medium">{label}</div>
          {description && <div className="text-xs text-muted-foreground">{description}</div>}
          {reason && <UnavailableReason reason={reason} />}
        </div>
        <div className="shrink-0">
          <ProviderRefControl def={def} value={value} providers={providersData} />
        </div>
      </div>
    );
  }

  if (def.type === "list") {
    // Fase 5a fix 1: `provider_category` è ora dichiarato dal backend anche
    // per "list" (prima solo per "provider_ref"), quindi letto qui allo
    // stesso identico modo — nessuna mappa chiave->categoria lato frontend.
    const providers = providersForCategory(providersData, def.provider_category);
    // Riordinare un solo provider (o nessuno) non ha senso: stessa soglia
    // che GeneralTab.tsx applicava prima di Fase 5a.
    if (providers.length <= 1) return null;
    const order = mergeProviderOrder(value as string[] | undefined, providers);
    return (
      <div className="space-y-2">
        <div>
          <p className="text-sm font-medium">{label}</p>
          {description && <p className="text-xs text-muted-foreground mt-0.5">{description}</p>}
          {reason && <UnavailableReason reason={reason} />}
        </div>
        <ProviderOrderList
          order={order}
          providers={providers}
          onChange={onChange}
          disabled={isLockedUnavailable(def, value)}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0 sm:pr-4">
        <div className="text-sm font-medium">{label}</div>
        {description && <div className="text-xs text-muted-foreground">{description}</div>}
        {def.provider_capability && (
          <CapabilityProviders capability={def.provider_capability} providers={providersData} />
        )}
        {reason && <UnavailableReason reason={reason} />}
      </div>
      <div className="shrink-0">
        <SettingControl def={def} value={value} onChange={onChange} disabled={isLockedUnavailable(def, value)} />
      </div>
    </div>
  );
}

/** Motivo di indisponibilità di UNA voce — già una stringa leggibile costruita
 * dal backend (categoria/label dichiarate, mai un nome di provider hardcoded
 * qui): renderizzata così com'è, mai ricostruita o tradotta lato frontend. */
function UnavailableReason({ reason }: { reason: string }) {
  return (
    <p className="mt-1 flex items-center gap-1 text-xs text-amber-600 dark:text-amber-500">
      <Info className="w-3.5 h-3.5 shrink-0" />
      {reason}
    </p>
  );
}

/**
 * The providers a setting acts through: every metadata provider that declares
 * `capability` (`ProviderInfo.capabilities`), with the state of the viewer's
 * OWN account there (`personal`) — connected, connected without the permission
 * (`missing_permissions`: reconnect), or not connected. A shared
 * account does not count: such a setting acts only on the viewer's accounts.
 * Driven by the API, never by a provider name.
 */
export function CapabilityProviders({
  capability,
  providers,
}: {
  capability: string;
  providers: ProvidersResponse | undefined;
}) {
  const { t } = useI18n();
  if (!providers) return null;
  const supporting = providers.metadata.filter((p) => p.capabilities?.includes(capability));
  if (supporting.length === 0) {
    return <p className="mt-1 text-xs text-muted-foreground">{t("settings.capability.none")}</p>;
  }
  const stateOf = (p: ProviderInfo) => {
    if (!p.personal?.connected) return { key: "notConnected", warn: false } as const;
    if (p.personal.missing_permissions.includes(capability)) return { key: "reconnect", warn: true } as const;
    return { key: "connected", warn: false } as const;
  };
  return (
    <div className="mt-1 text-xs text-muted-foreground">
      <span>{t("settings.capability.supportedBy")}:</span>
      <ul className="mt-0.5 space-y-0.5">
        {supporting.map((p) => {
          const state = stateOf(p);
          return (
            <li key={p.name} className={state.warn ? "text-amber-600 dark:text-amber-500" : undefined}>
              <span className="font-medium">{p.label}</span> — {t(`settings.capability.${state.key}`)}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ------------------------------------------------------------------
// Widget d'azione del gruppo "library_sync" — invariati da AdvancedTab.tsx.
// ------------------------------------------------------------------

function FullSyncVerify() {
  const { t } = useI18n();
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    setStatus(t("settings.startingVerification"));
    try {
      const { task_id } = await startFullSyncVerify();
      // poll
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const s = await getFullSyncVerifyStatus(task_id);
        if (s.ready) {
          if (s.result?.full_sync) {
            setStatus(t("settings.fullSyncVerified"));
            toast.success(t("settings.fullSyncActivatedToast"));
          } else if (s.result?.status === "error") {
            setStatus(t("settings.verifyError", { detail: s.result.detail ?? t("settings.verifyFailedDefault") }));
          } else {
            setStatus(t("settings.testFileMissing"));
          }
          setBusy(false);
          return;
        }
        setStatus(t("settings.verifyingProgress", { seconds: (i + 1) * 3 }));
      }
      setStatus(t("settings.verifyTimeout"));
    } catch {
      setStatus(t("settings.verifyStartError"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-dashed p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm">
          <div className="font-medium">{t("settings.fullSyncVerifyTitle")}</div>
          <div className="text-xs text-muted-foreground">
            {t("settings.fullSyncVerifyDescription")}
          </div>
        </div>
        <Button size="sm" variant="secondary" disabled={busy} onClick={run}>
          {busy ? t("settings.inProgress") : t("settings.verify")}
        </Button>
      </div>
      {status && <div className="mt-2 text-xs text-muted-foreground">{status}</div>}
    </div>
  );
}
