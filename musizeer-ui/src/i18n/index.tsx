import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { messages, type Locale, type MessageKey, type Plural } from "./messages";

type Vars = Record<string, string | number>;

interface I18nContextValue {
  locale: Locale;
  /**
   * User-initiated change. Persists to the device-local cache and applies
   * immediately. Callers signed in to a real account are responsible for
   * also pushing the value to the server (see PreferencesTab) — this setter
   * only owns the local/device half, so it stays correct for
   * signed-out/auth-disabled use too.
   */
  setLocale: (l: Locale) => void;
  /**
   * Reconciliation entry point: applies a value that already lives
   * server-side (used by PreferencesSync). Updates the local cache too, only
   * so the next load's first paint is right before any network request
   * resolves — never writes back to the server. Using `setLocale` here would
   * create a feedback loop.
   */
  applyServerLocale: (l: Locale) => void;
  t: (key: MessageKey, vars?: Vars) => string;
  /**
   * A key that is only known at runtime (a `label_key` or `error_code` sent
   * by the backend): its translation, or `fallback` when the dictionary has
   * no such key.
   */
  tx: (key: string, fallback: string, vars?: Vars) => string;
}

export const LOCALE_STORAGE_KEY = "musizerr-lang";

// Mirrors the declared default for the "language" setting in
// core/settings_catalog.py. Kept here (not fetched) because it also has to
// serve as the fallback for the signed-out / auth-disabled path, which never
// talks to the server at all.
export const DEFAULT_LOCALE: Locale = "en";

function detectLocale(): Locale {
  try {
    const stored = localStorage.getItem(LOCALE_STORAGE_KEY);
    if (stored === "en" || stored === "it") return stored;
  } catch {
    /* localStorage non disponibile (SSR, private mode): usa il navigator */
  }
  const nav = typeof navigator !== "undefined" ? navigator.language : DEFAULT_LOCALE;
  return nav.toLowerCase().startsWith("it") ? "it" : DEFAULT_LOCALE;
}

/** Naviga il dizionario per dot-path; ritorna la chiave stessa se manca. */
function resolve(locale: Locale, key: string, vars?: Vars): string {
  return lookup(locale, key, vars) ?? key; // chiave mancante: mostra la chiave (dev-visible)
}

const pluralRules = new Map<Locale, Intl.PluralRules>();

function isPlural(node: unknown): node is Plural {
  return !!node && typeof node === "object" && typeof (node as Plural).other === "string";
}

/** The form of a plural message for `vars.count` (0 when it is missing). */
function pluralForm(locale: Locale, message: Plural, count: unknown): string {
  let rules = pluralRules.get(locale);
  if (!rules) {
    rules = new Intl.PluralRules(locale);
    pluralRules.set(locale, rules);
  }
  const n = Number(count);
  const category = rules.select(Number.isFinite(n) ? n : 0) as keyof Plural;
  return message[category] ?? message.other;
}

function lookup(locale: Locale, key: string, vars?: Vars): string | null {
  const parts = key.split(".");
  let node: unknown = messages[locale];
  for (const part of parts) {
    if (node && typeof node === "object" && part in (node as object)) {
      node = (node as Record<string, unknown>)[part];
    } else {
      return null;
    }
  }
  if (typeof node === "string") return node;
  return isPlural(node) ? pluralForm(locale, node, vars?.count) : null;
}

function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{\{(\w+)\}\}/g, (_, name) =>
    name in vars ? String(vars[name]) : `{{${name}}}`,
  );
}

const I18nContext = createContext<I18nContextValue>({
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
  applyServerLocale: () => {},
  t: (key) => key,
  tx: (_key, fallback) => fallback,
});

function persist(locale: Locale) {
  try {
    localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    /* best effort */
  }
}

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(detectLocale);

  const setLocale = useCallback((l: Locale) => {
    persist(l);
    setLocaleState(l);
  }, []);

  const applyServerLocale = useCallback((l: Locale) => {
    persist(l);
    setLocaleState(l);
  }, []);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const t = useCallback(
    (key: MessageKey, vars?: Vars) => interpolate(resolve(locale, key, vars), vars),
    [locale],
  );

  const tx = useCallback(
    (key: string, fallback: string, vars?: Vars) => interpolate(lookup(locale, key, vars) ?? fallback, vars),
    [locale],
  );

  const value = useMemo(
    () => ({ locale, setLocale, applyServerLocale, t, tx }),
    [locale, setLocale, applyServerLocale, t, tx],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export const useI18n = () => useContext(I18nContext);

export type { Locale, MessageKey };
