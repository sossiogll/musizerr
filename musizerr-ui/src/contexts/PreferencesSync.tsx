import { useEffect, useRef } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/contexts/AuthContext";
import { useCan } from "@/hooks/useCapabilities";
import { getSettingsValues, patchSettings } from "@/lib/api-client";
import { useTheme, DEFAULT_THEME, THEME_STORAGE_KEY, type Theme } from "@/contexts/ThemeContext";
import { useI18n, DEFAULT_LOCALE, LOCALE_STORAGE_KEY, type Locale } from "@/i18n";

/**
 * "Is there a real account behind this session": the `settings_personal`
 * capability (a user or an admin; open mode has no personal profile). Every
 * place that decides between account-backed and device-local behavior (e.g.
 * AccountTab: identity block, language/theme through the API or straight
 * into ThemeContext/I18nContext) reuses this hook, so the decisions never
 * drift apart.
 */
export function useIsSignedIn(): boolean {
  // settings_personal is exactly "a real account owns this session": a user
  // or an admin. Open mode (sign-in off) has no personal profile.
  return useCan("settings_personal");
}

function readLocal(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/**
 * Bridges the account-level `language`/`theme` settings (server-side,
 * scope="user" in core/settings_catalog.py) with ThemeContext/I18nContext
 * (device-local, always available even signed out or with auth disabled).
 *
 * Reads GET /settings/values (query key "settings-values") — the only
 * settings path now that the legacy GET /config has been retired. The
 * settings UI (SettingsArea, the admin providers page) writes through PATCH /settings
 * and updates that SAME query key on success — so a theme/language change
 * made from the Account tab is picked up by this component immediately,
 * without a reload, exactly like a change made from here is picked up by
 * SettingsArea.
 *
 * GET /settings/values OMITS a scope="user" key entirely when this user has
 * never stored a row for it (see SettingsService.effective_values) — so
 * "missing from `values`" is an unambiguous "never chosen", distinct from
 * any real stored value, including one that happens to equal the declared
 * default. That is the only signal this component needs for one-time
 * adoption.
 *
 * Mounted once in root.tsx, inside AuthProvider so it can see
 * `user`/`authConfig`, and inside ThemeProvider/I18nProvider too, since
 * those wrap the whole app from main.tsx. No UI of its own.
 *
 * Deliberately inert when there is no real signed-in account: `authConfig`
 * disabled means every request resolves to the backend's synthetic system
 * user (see app/routes/deps.py::_SYSTEM_USER) — syncing there would make
 * every browser hitting this deployment share one global theme/language
 * instead of a per-device preference, which is worse than what we have
 * today. That is why `signedIn` is the `settings_personal` capability (a
 * real account), which open mode does not have.
 */
export function PreferencesSync() {
  const { user } = useAuth();
  const { applyServerTheme } = useTheme();
  const { applyServerLocale } = useI18n();
  const queryClient = useQueryClient();

  const signedIn = useIsSignedIn();

  const { data: values } = useQuery({
    queryKey: ["settings-values"],
    queryFn: getSettingsValues,
    staleTime: 5 * 60 * 1000,
    // Only an account has values to sync; a guest would just get a 401.
    enabled: signedIn,
  });

  const { mutate } = useMutation({
    mutationFn: (updates: Record<string, unknown>) => patchSettings(updates),
    onSuccess: (data) => {
      queryClient.setQueryData(["settings-values"], data);
    },
  });

  // React Query has no idea a login/logout changes which identity a request
  // like GET /settings/values resolves for. Without this, a login that
  // happens without a full page reload can leave the query stuck on
  // whatever it resolved to while signed out (undefined, or a stale 401
  // error) — nothing would otherwise trigger a refetch under the new
  // identity. Fires once per null -> non-null transition; harmless if it
  // occasionally overlaps with a session already restored on cold start.
  const prevUserId = useRef<string | null>(null);
  useEffect(() => {
    const id = user?.id ?? null;
    if (id && !prevUserId.current) {
      queryClient.invalidateQueries({ queryKey: ["settings-values"] });
    }
    prevUserId.current = id;
  }, [user?.id, queryClient]);

  // One-time adoption + ongoing reconciliation, whenever a signed-in user's
  // settings values are (re)loaded.
  useEffect(() => {
    if (!signedIn || !values) return;

    const hasServerTheme = values.theme !== undefined;
    const hasServerLocale = values.language !== undefined;
    const localTheme = readLocal(THEME_STORAGE_KEY) as Theme | null;
    const localLocale = readLocal(LOCALE_STORAGE_KEY) as Locale | null;

    // Nothing stored server-side yet: keep showing this device's cached
    // preference (falling back to the declared default only if it has none
    // either) rather than flashing to the default and back once the adopted
    // value round-trips.
    applyServerTheme(hasServerTheme ? (values.theme as Theme) : localTheme ?? DEFAULT_THEME);
    applyServerLocale(hasServerLocale ? (values.language as Locale) : localLocale ?? DEFAULT_LOCALE);

    const updates: { theme?: Theme; language?: Locale } = {};
    if (!hasServerTheme && localTheme) updates.theme = localTheme;
    if (!hasServerLocale && localLocale) updates.language = localLocale;
    if (Object.keys(updates).length > 0) {
      // Once this resolves, the key is no longer missing from `values`, so
      // this branch naturally stops firing — no separate "already adopted"
      // marker needed.
      mutate(updates, {
        onError: () => {
          // Best effort — the adopted value is already applied above, so the
          // user doesn't lose it visually even if this particular write
          // fails. It will simply be retried next time `values` reloads,
          // since the key is still missing server-side.
        },
      });
    }
  }, [signedIn, values, applyServerTheme, applyServerLocale, mutate]);

  return null;
}
