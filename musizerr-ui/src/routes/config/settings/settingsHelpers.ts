import type { ProviderInfo, ProvidersResponse } from "@/types";
import type { SettingDef } from "@/lib/api-client";

// Le cinque categorie originarie più "identification" (Fase 5a fix 2: GET
// /providers ora copre ogni categoria che il registry conosce, la stessa
// lista di core/settings_resolver.py::PROVIDER_CATEGORY_GETTERS). Sono
// categorie di provider dichiarate dal core (il ruolo pluggable, non
// un'implementazione concreta) — la stessa classe di "nome" che
// `ProvidersResponse` già elenca come campi fissi, non un provider.
const CATEGORY_KEYS: (keyof ProvidersResponse)[] = [
  "metadata",
  "download",
  "lyrics",
  "storage",
  "sync",
  "identification",
];

type Translate = (key: string, fallback: string) => string;

/** Label and description of a setting in the viewer's language: the
 * dictionary entry under `label_key` / `description_key` (GET
 * /settings/schema), else the catalog text. */
export function settingLabel(def: SettingDef, tx: Translate): string {
  return def.label_key ? tx(def.label_key, def.label) : def.label;
}

export function settingDescription(def: SettingDef, tx: Translate): string {
  return def.description_key ? tx(def.description_key, def.description) : def.description;
}

export function providersForCategory(
  data: ProvidersResponse | undefined,
  category: string | null | undefined,
): ProviderInfo[] {
  if (!data || !category) return [];
  return CATEGORY_KEYS.includes(category as keyof ProvidersResponse)
    ? data[category as keyof ProvidersResponse]
    : [];
}

/** Un provider di GRUPPO (una voce per provider registrato, es. il gruppo
 * "myprovider" nell'area providers) non compare in `taxonomy.groups` — la sua
 * label vive in `ProviderInfo.label`, mai duplicata nel core. Cerca in tutte
 * le categorie perché un provider può comparire sotto più di una. */
export function findProviderByName(data: ProvidersResponse | undefined, name: string): ProviderInfo | undefined {
  if (!data) return undefined;
  for (const key of CATEGORY_KEYS) {
    const found = data[key].find((p) => p.name === name);
    if (found) return found;
  }
  return undefined;
}

/** Nomi provider in ordine: quelli già in `order` (nel loro ordine, filtrati
 * a quelli tuttora installati), poi quelli non ancora presenti (installati
 * ma mai riordinati), nell'ordine restituito da GET /providers.
 * Il server restituisce già l'ordine EFFETTIVO (completo, mai vuoto con
 * provider installati): qui resta solo come rete di sicurezza per un provider
 * comparso fra la lettura dei valori e quella dei provider. */
export function mergeProviderOrder(order: string[] | undefined, providers: ProviderInfo[]): string[] {
  const known = new Set(providers.map((p) => p.name));
  const kept = (order ?? []).filter((name) => known.has(name));
  const rest = providers.map((p) => p.name).filter((name) => !kept.includes(name));
  return [...kept, ...rest];
}

/**
 * Fase 5c, Task 3 — etichette da mostrare come badge su una card di provider:
 * la label di OGNI impostazione `provider_ref` la cui `provider_category`
 * corrisponde a `category` e il cui valore attuale è esattamente
 * `providerName`. Guidato interamente da schema + valori — nessun nome di
 * ruolo o di provider hardcoded qui: una futura impostazione provider_ref
 * ottiene il suo badge senza alcuna modifica al frontend.
 */
export function activeProviderRefBadges(
  settings: SettingDef[] | undefined,
  values: Record<string, unknown> | undefined,
  category: string,
  providerName: string,
): string[] {
  if (!settings || !values) return [];
  return settings
    .filter((s) => s.type === "provider_ref" && s.provider_category === category && values[s.key] === providerName)
    .map((s) => s.label);
}

/**
 * `visible_when`: coppie (altra chiave, valore richiesto) che devono valere
 * TUTTE perché l'impostazione sia mostrata. `valueOf` deve riflettere lo
 * stato attualmente a schermo — incluse le modifiche non ancora salvate —
 * così un toggle rivela subito le impostazioni che dipendono da lui, prima
 * di qualunque salvataggio (vedi SettingsArea.valueOf).
 */
export function isSettingVisible(def: SettingDef, valueOf: (key: string) => unknown): boolean {
  if (!def.visible_when || def.visible_when.length === 0) return true;
  return def.visible_when.every(([key, expected]) => valueOf(key) === expected);
}

// Stesso prefisso di `core/settings_resolver.py::namespace_provider_key` —
// una chiave di provider è sempre "provider.<nome>.<short_key>" (almeno due
// punti dopo il prefisso). Riconosciuto dalla FORMA della chiave, mai da un
// elenco di nomi di provider: così un nuovo provider non richiede mai una
// modifica qui (Fase 5b, fix strutturale — le card di provider sono l'unico
// editor delle loro impostazioni, `SettingsArea` non le duplica più).
const _PROVIDER_KEY_PREFIX = "provider.";

export function isProviderNamespacedKey(key: string): boolean {
  if (!key.startsWith(_PROVIDER_KEY_PREFIX)) return false;
  const rest = key.slice(_PROVIDER_KEY_PREFIX.length);
  // Serve un secondo punto dopo il nome del provider: "provider.xxx" da solo
  // (senza un secondo punto) non è una vera chiave namespaced — stesso caso
  // limite di `split_provider_key` lato backend.
  return rest.includes(".");
}

/**
 * Entry di `GET /settings/schema` che appartengono a UN provider specifico
 * (chiave "provider.<providerName>.<short_key>") — usato dalla card di
 * provider (the admin providers page) per costruire il proprio editor di impostazioni,
 * genericamente, dallo stesso schema dichiarato che alimenta SettingsArea.
 * Il confronto include il punto finale dopo il nome così "myprovider" non
 * intercetta mai le chiavi di un ipotetico "myprovider2" (prefisso, non
 * substring). Una risposta non ancora caricata (`settings` undefined)
 * produce semplicemente un elenco vuoto — mai un errore — cosi' la card
 * regge la stessa corsa al caricamento di GET /providers.
 */
export function providerSettingsSchema(
  settings: SettingDef[] | undefined,
  providerName: string,
): SettingDef[] {
  if (!settings) return [];
  const prefix = `${_PROVIDER_KEY_PREFIX}${providerName}.`;
  return settings.filter((s) => s.key.startsWith(prefix));
}

// ---------------------------------------------------------------------------
// Fase 5c, Task 1 — disponibilità di un'impostazione (`available` /
// `unavailable_reason`, esposti additivamente da GET /settings/schema).
// ---------------------------------------------------------------------------

/** None, stringa vuota o lista vuota — mai un valore "spento" legittimo come
 * `false` o `0` (stessa nozione di "vuoto" di `core.settings_resolver.
 * is_empty_value", qui solo per decidere se un riferimento/ordine è "spento",
 * mai per un numero o un enum). */
function isEmptyish(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string" && value === "") return true;
  if (Array.isArray(value) && value.length === 0) return true;
  return false;
}

/**
 * Un'impostazione NON disponibile va bloccata del tutto SOLO se non c'è nulla
 * da spegnere — altrimenti l'utente deve poter continuare a interagire con
 * essa per tornare al suo stato "spento" (l'unica scrittura che il server
 * accetta comunque, vedi CLAUDE.md/settings_service). Il catalogo non espone
 * MAI il valore di default dichiarato al frontend (vedi
 * `test_public_catalog_never_leaks_values`), quindi "spento" è dedotto dal
 * TIPO, non da un default letto dal server:
 *   - "bool": un flag è "spento" quando non è `true` — ogni impostazione
 *     bool con requisiti nel catalogo di oggi ha default `false` (un flag che
 *     RICHIEDE un prerequisito non ha senso acceso di default).
 *   - "provider_ref" / "list": "spento" è un riferimento vuoto/nessun ordine
 *     — la stessa nozione di "vuoto" che il backend applica al requisito
 *     "setting_set" (`is_empty_value`).
 *   - qualunque altro tipo (int/float/string/enum): un parametro numerico o
 *     testuale non ha un singolo stato "spento" riconoscibile qui — bloccato
 *     del tutto quando non disponibile, indipendentemente dal valore.
 */
export function isLockedUnavailable(def: SettingDef, value: unknown): boolean {
  if (def.available !== false) return false;
  if (def.type === "bool") return value !== true;
  if (def.type === "provider_ref" || def.type === "list") return isEmptyish(value);
  return true;
}
