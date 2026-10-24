import { describe, it, expect, vi, beforeEach } from "vitest";
import { viewerStatus } from "@/test/viewer";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider, useTheme } from "@/contexts/ThemeContext";
import { I18nProvider, useI18n } from "@/i18n";
import { PreferencesSync } from "./PreferencesSync";

// PreferencesSync bridges AuthContext (identity) and GET/PATCH /settings
// (Fase 5a: the same endpoints the new SettingsArea renderer uses) into
// ThemeContext/I18nContext. Mocking the API functions and AuthContext lets
// these tests drive the bridge directly, against the REAL Theme/I18n/
// react-query providers, without hitting a network.
const mockUseAuth = vi.fn();
const mockGetSettingsValues = vi.fn();
const mockPatchSettings = vi.fn();

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("@/lib/api-client", () => ({
  getSettingsValues: (...args: unknown[]) => mockGetSettingsValues(...args),
  patchSettings: (...args: unknown[]) => mockPatchSettings(...args),
}));

function Probe() {
  const { theme } = useTheme();
  const { locale } = useI18n();
  return (
    <div data-testid="probe">
      {theme}:{locale}
    </div>
  );
}

function renderWithSync() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <I18nProvider>
          <PreferencesSync />
          <Probe />
        </I18nProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  mockUseAuth.mockReset();
  mockGetSettingsValues.mockReset();
  mockPatchSettings.mockReset();
  mockGetSettingsValues.mockResolvedValue({});
  mockPatchSettings.mockResolvedValue({});
});

describe("PreferencesSync", () => {
  it("signed out: theme/locale keep coming from local storage, untouched", () => {
    localStorage.setItem("musizerr-theme", "dark");
    localStorage.setItem("musizerr-lang", "it");
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("guest"), user: null });

    renderWithSync();

    expect(screen.getByTestId("probe").textContent).toBe("dark:it");
  });

  it("auth disabled: the owner of the instance syncs like any account, with the settings stored for the instance's user", async () => {
    localStorage.setItem("musizerr-theme", "light");
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("open"), user: null });
    mockGetSettingsValues.mockResolvedValue({ theme: "dark", language: "it" });

    renderWithSync();

    await waitFor(() => expect(screen.getByTestId("probe").textContent).toBe("dark:it"));
    expect(mockPatchSettings).not.toHaveBeenCalled();
  });

  it("a guest never reads settings (they would be a 401)", async () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("guest"), user: null });
    mockGetSettingsValues.mockResolvedValue({ theme: "dark", language: "it" });

    renderWithSync();

    await new Promise((r) => setTimeout(r, 50));
    expect(mockGetSettingsValues).not.toHaveBeenCalled();
    expect(mockPatchSettings).not.toHaveBeenCalled();
  });

  it("signed in: a server value overrides what's cached in local storage", async () => {
    localStorage.setItem("musizerr-theme", "light");
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: { id: "u1" } });
    mockGetSettingsValues.mockResolvedValue({ theme: "dark", language: "it" });

    renderWithSync();

    await waitFor(() => expect(screen.getByTestId("probe").textContent).toBe("dark:it"));
    expect(mockPatchSettings).not.toHaveBeenCalled();
    // The server value also becomes the device's cache, for next load's first paint.
    expect(localStorage.getItem("musizerr-theme")).toBe("dark");
  });

  it("first sign-in: adopts the local value once, because the key is missing from GET /settings/values", async () => {
    localStorage.setItem("musizerr-theme", "dark"); // this device already had a preference
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: { id: "u1" } });
    mockGetSettingsValues.mockResolvedValue({ language: "en" }); // no "theme" key: never chosen
    mockPatchSettings.mockResolvedValue({ language: "en", theme: "dark" });

    renderWithSync();

    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledWith({ theme: "dark" }));
    expect(screen.getByTestId("probe").textContent).toBe("dark:en");
  });

  it("does not overwrite a stored value that happens to equal the default", async () => {
    // This is the bug the presence-based check fixes: a user who genuinely
    // chose "system" (the declared default) must not have it clobbered by a
    // stale/different local cache on a device that used to show "dark".
    localStorage.setItem("musizerr-theme", "dark");
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: { id: "u1" } });
    mockGetSettingsValues.mockResolvedValue({ theme: "system", language: "en" }); // present: chosen explicitly

    renderWithSync();

    await waitFor(() => expect(screen.getByTestId("probe").textContent).toBe("system:en"));
    expect(mockPatchSettings).not.toHaveBeenCalled();
  });

  it("a save from the settings UI (same query key) is reflected immediately, no reload needed", async () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: { id: "u1" } });
    mockGetSettingsValues.mockResolvedValue({ theme: "light", language: "en" });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <I18nProvider>
            <PreferencesSync />
            <Probe />
          </I18nProvider>
        </ThemeProvider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByTestId("probe").textContent).toBe("light:en"));

    // Simulate SettingsArea's onSuccess: it writes the PATCH /settings
    // response straight into the ["settings-values"] cache that this
    // component also reads.
    queryClient.setQueryData(["settings-values"], { theme: "dark", language: "en" });

    await waitFor(() => expect(screen.getByTestId("probe").textContent).toBe("dark:en"));
  });
});
