import { describe, it, expect, vi, beforeEach } from "vitest";
import { viewerStatus } from "@/test/viewer";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { I18nProvider } from "@/i18n";
import { AccountTab } from "./AccountTab";
import type { ConfigSchemaResponse, SettingDef } from "@/lib/api-client";
import type { ProviderInfo, ProvidersResponse, User } from "@/types";

// "Also remove the like where it came from": a personal setting (scope
// "user") shown in Settings › Account, with the providers that support it and
// the state of the viewer's OWN account on each (Settings › Linked accounts
// asks to reconnect an account connected without the permission, see
// LinkedAccountsTab.test.tsx). Fixtures are the
// exact JSON the backend answers (core/settings_catalog.py::to_public,
// routes/config.py::_provider_info).
const mockUseAuth = vi.fn();
const mockGetSettingsSchema = vi.fn();
const mockGetSettingsValues = vi.fn();
const mockGetProviders = vi.fn();
const mockPatchSettings = vi.fn();

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
    revokeAllAuth: vi.fn(),
    getOAuthAuthorizeUrl: vi.fn(),
    revokeAuth: vi.fn(),
  };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const KEY = "unlike_on_origin_providers";

// GET /settings/schema, the entry as the backend serializes it.
const UNLIKE_DEF: SettingDef = {
  key: KEY,
  type: "bool",
  group: "account",
  area: "account",
  label: "Togli il like anche dal provider d'origine",
  description:
    "Quando togli un preferito, toglie il like anche sul tuo account dei provider da cui era stato importato, solo dove l'API lo permette. Agisce soltanto sui tuoi account collegati, mai su quelli condivisi.",
  label_key: "settings.labels.unlike_on_origin_providers",
  description_key: "settings.descriptions.unlike_on_origin_providers",
  provider_capability: "unlike_saved_track",
  admin_only: false,
  scope: "user",
  options: null,
  minimum: null,
  maximum: null,
  step: null,
  secret: false,
  provider_category: null,
  required: true,
  hidden: false,
  option_label_keys: null,
  confirm: null,
  visible_when: null,
  available: true,
  unavailable_reason: null,
};

function schema(): ConfigSchemaResponse {
  return {
    settings: [UNLIKE_DEF],
    taxonomy: {
      areas: [{ key: "account", label_key: "settings.areas.account", label: "Account" }],
      groups: [
        {
          key: "account",
          label_key: "settings.groups.account",
          label: "Account",
          description_key: "settings.group_descriptions.account",
          description: "",
          area: "account",
        },
      ],
    },
  };
}

// GET /providers, metadata entries as _provider_info builds them.
function ownAccount(connected: boolean, missing: string[] = []): ProviderInfo["personal"] {
  return { connected, username: null, missing_permissions: missing, capabilities: ["user_library", "unlike_saved_track"], login: null };
}

function oauthProvider(overrides: Partial<ProviderInfo>): ProviderInfo {
  return {
    name: "origin",
    label: "Origin",
    auth_flow: "oauth2",
    account_model: "personal_with_shared",
    personal: ownAccount(true),
    shared: { connected: true, username: null },
    authenticated: true,
    usable: true,
    client_id: "app-client",
    config: {},
    config_valid: true,
    also_registered_in: [],
    capabilities: ["user_library", "unlike_saved_track"],
    ...overrides,
  };
}

function providers(): ProvidersResponse {
  return {
    metadata: [
      oauthProvider({ name: "origin", label: "Origin" }),
      oauthProvider({ name: "older", label: "Older", personal: ownAccount(true, ["unlike_saved_track"]) }),
      oauthProvider({ name: "shared", label: "Shared", personal: ownAccount(false) }),
      oauthProvider({ name: "readonly", label: "Read Only", capabilities: ["user_library"] }),
    ],
    download: [],
    lyrics: [],
    storage: [],
    sync: [],
    identification: [],
  };
}

const alice: User = { id: "u1", username: "alice", email: null, role: "user", is_active: true };

function wrap(ui: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <I18nProvider>{ui}</I18nProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("musizeer-lang", "en");
  mockUseAuth.mockReset();
  mockGetSettingsSchema.mockReset().mockResolvedValue(schema());
  // Never chosen: the key is absent from GET /settings/values.
  mockGetSettingsValues.mockReset().mockResolvedValue({});
  mockGetProviders.mockReset().mockResolvedValue(providers());
  mockPatchSettings.mockReset().mockImplementation(async (updates: Record<string, unknown>) => updates);
});

describe("Settings › Account: also remove the like where it came from", () => {
  it("is off until chosen, says what it does and lists the supporting providers with the viewer's own account state", async () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: alice, logout: vi.fn() });

    wrap(<AccountTab />);

    const toggle = await screen.findByRole("switch", { name: "Also remove the like where it came from" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText(/only acts on the accounts you connected yourself, never on shared ones/)).toBeInTheDocument();

    const list = await screen.findByRole("list");
    const items = within(list).getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual([
      "Origin — your account is connected",
      "Older — reconnect your account in Linked accounts to allow it",
      // A shared system account is not the viewer's: it does not count.
      "Shared — connect your account in Linked accounts to use it",
    ]);
    // A provider without the capability is not listed.
    expect(within(list).queryByText("Read Only")).not.toBeInTheDocument();
  });

  it("turning it on saves only this key, as the viewer's own choice", async () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: alice, logout: vi.fn() });

    wrap(<AccountTab />);

    fireEvent.click(await screen.findByRole("switch", { name: "Also remove the like where it came from" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledTimes(1));
    expect(mockPatchSettings.mock.calls[0][0]).toEqual({ [KEY]: true });
    await waitFor(() =>
      expect(screen.getByRole("switch", { name: "Also remove the like where it came from" })).toHaveAttribute(
        "aria-checked",
        "true",
      ),
    );
  });

  it("reads back a saved choice and speaks Italian", async () => {
    localStorage.setItem("musizeer-lang", "it");
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("admin"), user: { ...alice, role: "admin" }, logout: vi.fn() });
    mockGetSettingsValues.mockResolvedValue({ [KEY]: true });

    wrap(<AccountTab />);

    const toggle = await screen.findByRole("switch", { name: "Togli il like anche dal provider d'origine" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/Agisce solo sugli account che hai collegato tu/)).toBeInTheDocument();
    expect(await screen.findByText(/ricollega il tuo account in Account collegati per permetterlo/)).toBeInTheDocument();
  });

  it("says so when no installed provider supports it", async () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: alice, logout: vi.fn() });
    mockGetProviders.mockResolvedValue({
      ...providers(),
      metadata: [oauthProvider({ name: "readonly", label: "Read Only", capabilities: ["user_library"] })],
    });

    wrap(<AccountTab />);

    expect(await screen.findByText("None of the installed providers supports this yet.")).toBeInTheDocument();
  });

  it("is shown in open mode too: the owner of the instance has personal settings, saved as their own", async () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("open"), user: null, logout: vi.fn() });

    wrap(<AccountTab />);

    fireEvent.click(await screen.findByRole("switch", { name: "Also remove the like where it came from" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledTimes(1));
    expect(mockPatchSettings.mock.calls[0][0]).toEqual({ [KEY]: true });
  });
});
