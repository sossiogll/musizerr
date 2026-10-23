import { describe, it, expect, vi, beforeEach } from "vitest";
import { viewerStatus } from "@/test/viewer";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { I18nProvider } from "@/i18n";
import { AccountTab } from "./AccountTab";
import type { ConfigSchemaResponse, SettingDef } from "@/lib/api-client";
import type { ProvidersResponse, User } from "@/types";

// AccountTab is exercised here against the REAL ThemeProvider/I18nProvider —
// the same approach PreferencesSync.test.tsx uses. Only AuthContext and the
// API layer are mocked.
const mockUseAuth = vi.fn();
const mockGetSettingsSchema = vi.fn();
const mockGetSettingsValues = vi.fn();
const mockGetProviders = vi.fn();
const mockPatchSettings = vi.fn();
const mockRevokeAllAuth = vi.fn();

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("@/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client");
  return {
    ...actual,
    getSettingsSchema: (...a: unknown[]) => mockGetSettingsSchema(...a),
    getSettingsValues: (...a: unknown[]) => mockGetSettingsValues(...a),
    getProviders: (...a: unknown[]) => mockGetProviders(...a),
    patchSettings: (...a: unknown[]) => mockPatchSettings(...a),
    revokeAllAuth: (...a: unknown[]) => mockRevokeAllAuth(...a),
  };
});

function def(overrides: Partial<SettingDef> & Pick<SettingDef, "key" | "type">): SettingDef {
  return {
    group: "account",
    area: "account",
    label: overrides.key,
    description: "",
    admin_only: false,
    options: null,
    minimum: null,
    maximum: null,
    step: null,
    secret: false,
    provider_category: null,
    required: true,
    visible_when: null,
    ...overrides,
  };
}

// Mirrors the real catalog's "account" group (core/settings_catalog.py):
// language/theme are scope="user" (owner-less with no signed-in account),
// metadata_provider_priority is scope="system_with_user_override" (always
// has a global default to edit). "personal_flag" is a hypothetical future
// scope="user" setting, to show the generic handling. There is no auto-like
// setting any more: a user's download always adds to favorites.
function schema(): ConfigSchemaResponse {
  return {
    settings: [
      def({ key: "language", type: "enum", label: "Language", scope: "user", options: ["en", "it"] }),
      def({ key: "theme", type: "enum", label: "Theme", scope: "user", options: ["light", "dark", "system"] }),
      def({ key: "personal_flag", type: "bool", label: "Personal flag", scope: "user" }),
      def({
        key: "metadata_provider_priority",
        type: "list",
        label: "Metadata priority",
        scope: "system_with_user_override",
        provider_category: "metadata",
      }),
    ],
    taxonomy: {
      areas: [{ key: "account", label_key: "settings.areas.account", label: "Account" }],
      groups: [{ key: "account", label_key: "settings.groups.account", label: "Account", description_key: "settings.group_descriptions.account", description: "", area: "account" }],
    },
  };
}

function providers(): ProvidersResponse {
  return {
    metadata: [
      { name: "a", label: "Provider A", auth_flow: "none", account_model: "none", personal: null, shared: null, authenticated: true, usable: true, config: {} },
      { name: "b", label: "Provider B", auth_flow: "none", account_model: "none", personal: null, shared: null, authenticated: true, usable: true, config: {} },
    ],
    download: [],
    lyrics: [],
    storage: [],
    sync: [],
    identification: [],
  };
}

function fakeUser(): User {
  return { id: "u1", username: "alice", email: "alice@example.com", role: "user", is_active: true };
}

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <I18nProvider>
          <AccountTab />
        </I18nProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  mockUseAuth.mockReset();
  mockGetSettingsSchema.mockReset();
  mockGetSettingsValues.mockReset();
  mockGetProviders.mockReset();
  mockPatchSettings.mockReset();
  mockRevokeAllAuth.mockReset();

  mockGetSettingsSchema.mockResolvedValue(schema());
  mockGetProviders.mockResolvedValue(providers());
  mockGetSettingsValues.mockResolvedValue({ metadata_provider_priority: [] });
});

describe("AccountTab", () => {
  it("auth disabled: no identity block, but the owner's personal settings are the account's own, stored on the server", async () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("open"), user: null, logout: vi.fn() });
    mockGetSettingsValues.mockResolvedValue({ theme: "system", metadata_provider_priority: [] });
    mockPatchSettings.mockResolvedValue({ theme: "dark", metadata_provider_priority: [] });

    renderTab();

    // Language, theme and every other scope="user" setting render through the
    // generic SettingsArea, labelled from the declared schema.
    await screen.findByText("Language");
    expect(screen.getByText("Theme")).toBeInTheDocument();
    expect(screen.getByText("Personal flag")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Provider A")).toBeInTheDocument());

    // No identity block at all: no username, no sign-out, no danger zone.
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.queryByText("Danger zone")).not.toBeInTheDocument();
    // The device-only card of the old open mode is gone.
    expect(screen.queryByText("Device preferences")).not.toBeInTheDocument();

    // A change is saved with PATCH /settings (the server keeps it for the instance's user).
    const selects = screen.getAllByRole("combobox");
    fireEvent.change(selects[1], { target: { value: "dark" } }); // Theme, rendered second
    fireEvent.click(await screen.findByRole("button", { name: "Save" }));
    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledWith({ theme: "dark" }));
  });

  it("signed in: identity block, danger zone, and account-scoped settings render exactly as before", async () => {
    mockUseAuth.mockReturnValue({
      authConfig: viewerStatus("user"),
      user: fakeUser(),
      logout: vi.fn(),
    });
    mockGetSettingsValues.mockResolvedValue({
      language: "en",
      theme: "system",
      personal_flag: false,
      metadata_provider_priority: [],
    });

    const { container } = renderTab();

    await screen.findByText("alice");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.getByText("Danger zone")).toBeInTheDocument();

    // A scope="user" setting IS shown for a real signed-in account.
    expect(await screen.findByText("Personal flag")).toBeInTheDocument();
    // The auto-like toggle is gone.
    expect(screen.queryByText(/auto-like|like automatically/i)).not.toBeInTheDocument();
    // Language/theme render through the generic SettingsArea (settings-backed).
    expect(screen.getByText("Language")).toBeInTheDocument();
    expect(screen.getByText("Theme")).toBeInTheDocument();
    expect(screen.getByText("Provider A")).toBeInTheDocument();

    // Same content width as the other Settings sections: no cap on the tab.
    expect(container.firstElementChild?.className).not.toMatch(/max-w-/);
  });
});
