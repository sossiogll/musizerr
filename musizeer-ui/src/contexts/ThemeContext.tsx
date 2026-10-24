import { createContext, useCallback, useContext, useEffect, useState } from "react";

export type Theme = "light" | "dark" | "system";

// Mirrors the declared default for the "theme" setting in
// core/settings_catalog.py. Kept here (not fetched) because it also has to
// serve as the fallback for the signed-out / auth-disabled path, which never
// talks to the server at all (see PreferencesSync for the account-settings
// bridge, which reuses this constant to spot "server hasn't got a value").
export const DEFAULT_THEME: Theme = "system";

export const THEME_STORAGE_KEY = "musizeer-theme";

interface ThemeContextValue {
  theme: Theme;
  /**
   * User-initiated change (e.g. clicking a theme button in PreferencesTab).
   * Persists to the device-local cache and applies immediately. Callers that
   * are signed in to a real account are responsible for also pushing the
   * value to the server (see PreferencesTab) — this setter only owns the
   * local/device half, so it stays correct for signed-out/auth-disabled use
   * too.
   */
  setTheme: (theme: Theme) => void;
  /**
   * Reconciliation entry point: applies a value that already lives
   * server-side (used by PreferencesSync). Updates the local cache as well,
   * purely so the next load's first paint has the right value before any
   * network request resolves — it never writes back to the server. Calling
   * `setTheme` from a server-reconciliation path would create a feedback
   * loop (apply -> looks like a user change -> re-persist -> re-apply...).
   */
  applyServerTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: DEFAULT_THEME,
  setTheme: () => {},
  applyServerTheme: () => {},
});

function getSystemPreference(): "light" | "dark" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  const resolved = theme === "system" ? getSystemPreference() : theme;
  document.documentElement.classList.toggle("dark", resolved === "dark");
}

function persist(theme: Theme) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    /* private mode / storage disabled: best effort */
  }
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return (stored === "light" || stored === "dark" || stored === "system") ? stored : DEFAULT_THEME;
  });

  const setTheme = useCallback((t: Theme) => {
    persist(t);
    setThemeState(t);
  }, []);

  const applyServerTheme = useCallback((t: Theme) => {
    persist(t);
    setThemeState(t);
  }, []);

  // Apply on mount and when theme changes
  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // Listen for system preference changes when in "system" mode
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => {
      if (theme === "system") applyTheme("system");
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme]);

  return (
    <ThemeContext.Provider value={{ theme, setTheme, applyServerTheme }}>
      {children}
    </ThemeContext.Provider>
  );
}

export const useTheme = () => useContext(ThemeContext);
