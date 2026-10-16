import { describe, it, expect, vi, beforeEach } from "vitest";
import { viewerStatus } from "@/test/viewer";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/i18n";
import { ProviderCard, ProviderGroup } from "./providers";
import type { ProviderInfo } from "@/types";
import type { ConfigSchemaResponse, SettingDef } from "@/lib/api-client";

const mockUseAuth = vi.fn();
const mockPatchSettings = vi.fn();

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockSaveCredentials = vi.fn();
const mockTestProviderConnection = vi.fn();
const mockRevokeAuth = vi.fn();
const mockGetOAuthAuthorizeUrl = vi.fn();

vi.mock("@/lib/api-client", () => ({
  getProviders: vi.fn(),
  getSettingsSchema: vi.fn(),
  getSettingsValues: vi.fn(),
  saveApiKey: vi.fn(),
  saveOAuthClient: vi.fn(),
  deleteOAuthClient: vi.fn(),
  saveCredentials: (...a: unknown[]) => mockSaveCredentials(...a),
  revokeAuth: (...a: unknown[]) => mockRevokeAuth(...a),
  getOAuthAuthorizeUrl: (...a: unknown[]) => mockGetOAuthAuthorizeUrl(...a),
  patchSettings: (...a: unknown[]) => mockPatchSettings(...a),
  testProviderConnection: (...a: unknown[]) => mockTestProviderConnection(...a),
  deleteProviderAccount: vi.fn(),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

function provider(overrides: Partial<ProviderInfo> = {}): ProviderInfo {
  return {
    name: "fakeprov",
    label: "Fake Provider",
    auth_flow: "none",
    account_model: "none",
    personal: null,
    shared: null,
    authenticated: false,
    usable: false,
    config: {},
    ...overrides,
  };
}

// A single "string" setting namespaced to `fakeprov`, exactly the shape
// GET /settings/schema returns for a provider-declared field ("port"/"url"/
// etc. all come back as key: "provider.<name>.<short_key>", type "string" —
// never the old legacy `config_schema` wire vocabulary the card no longer
// reads).
function providerFieldDef(overrides: Partial<SettingDef> & Pick<SettingDef, "key"> = { key: "provider.fakeprov.url" }): SettingDef {
  return {
    type: "string",
    group: "fakeprov",
    area: "providers",
    label: "URL",
    description: "",
    admin_only: false,
    options: null,
    minimum: null,
    maximum: null,
    step: null,
    secret: false,
    provider_category: null,
    required: false,
    visible_when: null,
    ...overrides,
  };
}

function schemaWith(...settings: SettingDef[]): ConfigSchemaResponse {
  return {
    settings,
    taxonomy: { areas: [], groups: [] },
  };
}

function renderCard(
  p: ProviderInfo,
  schemaData?: ConfigSchemaResponse,
  valuesData?: Record<string, unknown>,
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <ProviderCard provider={p} schemaData={schemaData} valuesData={valuesData} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockUseAuth.mockReturnValue({ authConfig: viewerStatus("admin") });
  mockPatchSettings.mockReset();
  mockPatchSettings.mockResolvedValue({});
});

describe("ProviderCard — settings fields sourced from GET /settings/schema", () => {
  it("re-syncs an untouched field on a data refresh but keeps an in-progress edit", () => {
    const initial = provider();
    const schema = schemaWith(
      providerFieldDef({ key: "provider.fakeprov.url", label: "URL" }),
      providerFieldDef({ key: "provider.fakeprov.limit", label: "Limit" }),
    );
    const { rerender } = renderCard(initial, schema, {
      "provider.fakeprov.url": "http://old",
      "provider.fakeprov.limit": "10",
    });

    // The card starts collapsed for a "none"-auth provider that's already
    // in a settled state — open it to reach the settings fields.
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));

    const urlInput = screen.getByDisplayValue("http://old") as HTMLInputElement;
    // Simulate the user mid-edit on "url" — an unsaved, in-progress change.
    fireEvent.change(urlInput, { target: { value: "http://mine" } });
    expect(screen.getByDisplayValue("http://mine")).toBeInTheDocument();

    // A refetch of GET /settings/values (e.g. invalidated by another card,
    // or by this very save elsewhere) brings back fresh data: "limit"
    // changed on the server, "url" did too — but the user never touched
    // "limit", while "url" is mid-edit here and must NOT be clobbered.
    // Unlike the old `provider.config`-seeded local state, this works with
    // no special resync effect at all: an untouched field simply reads
    // straight through to the fresh `valuesData` prop every render.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    rerender(
      <QueryClientProvider client={queryClient}>
        <I18nProvider>
          <ProviderCard
            provider={initial}
            schemaData={schema}
            valuesData={{ "provider.fakeprov.url": "http://server-new", "provider.fakeprov.limit": "20" }}
          />
        </I18nProvider>
      </QueryClientProvider>,
    );

    // Untouched field: re-synced to the fresh value from the refetch.
    expect(screen.getByDisplayValue("20")).toBeInTheDocument();
    // Field mid-edit: the user's in-progress typing survives the refetch.
    expect(screen.getByDisplayValue("http://mine")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("http://server-new")).not.toBeInTheDocument();
  });

  it("adopts the fresh value for a field once it is no longer mid-edit", () => {
    const initial = provider();
    const schema = schemaWith(providerFieldDef({ key: "provider.fakeprov.url", label: "URL" }));
    const { rerender } = renderCard(initial, schema, { "provider.fakeprov.url": "http://old" });
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));
    expect(screen.getByDisplayValue("http://old")).toBeInTheDocument();

    // No edits made at all — a plain refetch (e.g. after this card's own
    // successful save elsewhere invalidates the settings-values query) must
    // be reflected, not frozen at the value the card was first seeded with.
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    rerender(
      <QueryClientProvider client={queryClient}>
        <I18nProvider>
          <ProviderCard provider={initial} schemaData={schema} valuesData={{ "provider.fakeprov.url": "http://saved" }} />
        </I18nProvider>
      </QueryClientProvider>,
    );

    expect(screen.getByDisplayValue("http://saved")).toBeInTheDocument();
  });

  it("sends a text field holding digits as the typed string, never coerced to a number", async () => {
    // This was the very first defect found in this refactor: the old card
    // guessed types in the browser (`isNaN(Number(v)) ? v : Number(v)`), so
    // a text field whose current text happens to be all digits (a port, an
    // account id, ...) went out as a JSON number. The server now coerces by
    // the DECLARED type — the browser must send exactly what the user typed.
    const schema = schemaWith(providerFieldDef({ key: "provider.fakeprov.port", label: "Port" }));
    renderCard(provider(), schema, { "provider.fakeprov.port": "8080" });
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));

    const portInput = screen.getByDisplayValue("8080") as HTMLInputElement;
    fireEvent.change(portInput, { target: { value: "9090" } });

    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledWith({ "provider.fakeprov.port": "9090" }));
    const sent = mockPatchSettings.mock.calls[0][0];
    expect(typeof sent["provider.fakeprov.port"]).toBe("string");
  });

  it("allows clearing a field to an empty string", async () => {
    // The old card skipped any field whose current text was empty
    // (`if (v !== "") parsed[k] = ...`), so a field could never be cleared
    // once set. The generic path sends exactly what's on screen, including
    // an explicit empty string, and lets the server decide.
    const schema = schemaWith(providerFieldDef({ key: "provider.fakeprov.url", label: "URL" }));
    renderCard(provider(), schema, { "provider.fakeprov.url": "http://old" });
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));

    const urlInput = screen.getByDisplayValue("http://old") as HTMLInputElement;
    fireEvent.change(urlInput, { target: { value: "" } });

    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledWith({ "provider.fakeprov.url": "" }));
  });

  it("shows 'Not connected' for a no-login provider whose config is unusable", () => {
    // auth_flow "none" has no login step to fail, so `authenticated` is
    // always true — that alone made the dot lie for a provider like this
    // (e.g. a provider without the API key of its service). `usable` is the truthful signal.
    renderCard(provider({ authenticated: true, usable: false }));

    // In words, not a coloured dot with a tooltip.
    expect(screen.getByText("Not connected")).toBeInTheDocument();
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
  });

  it("shows 'Connected' for a no-login provider with a usable config", () => {
    renderCard(provider({ authenticated: true, usable: true }));

    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.queryByText("Not connected")).not.toBeInTheDocument();
  });
});

const OWN = { connected: false, username: null, missing_permissions: [], capabilities: [], login: null };
const LOGIN = [
  { key: "username", label: "Username", secret: false },
  { key: "password", label: "Password", secret: true },
];

// Administration › Providers manages the INSTANCE's accounts, by the model the
// provider declares: a personal account is each user's own (Settings › Linked
// accounts) and never appears here.
describe("Shared accounts — by the account model the provider declares", () => {
  beforeEach(() => {
    mockSaveCredentials.mockReset().mockResolvedValue({ success: true });
    mockRevokeAuth.mockReset().mockResolvedValue({ ok: true });
    mockTestProviderConnection.mockReset().mockResolvedValue({ app: null, account: { ok: true, detail: "Connected" } });
    mockGetOAuthAuthorizeUrl.mockReset();
  });

  it("says what the model means for the instance", () => {
    renderCard(provider({ account_model: "system", shared: { connected: false, username: null }, auth_flow: "api_key" }));

    expect(screen.getByText("Only the instance's account is used, managed here.")).toBeInTheDocument();
  });

  it("shows the connected instance login by name, never its secret, with Test and Remove", () => {
    renderCard(
      provider({
        auth_flow: "credentials",
        account_model: "personal_with_shared",
        credential_fields: LOGIN,
        authenticated: true,
        shared: { connected: true, username: "house" },
        personal: OWN,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));

    expect(screen.getByText("house")).toBeInTheDocument();
    expect(screen.getByText("••••••••")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Test" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove account" })).toBeInTheDocument();
    // Whose account a user connects is theirs.
    expect(screen.getByText("Users connect their own account in Settings › Linked accounts.")).toBeInTheDocument();
  });

  it("saves the instance login as the SHARED account, tests it, and keeps it when it works", async () => {
    renderCard(
      provider({
        auth_flow: "credentials",
        account_model: "system",
        credential_fields: LOGIN,
        shared: { connected: false, username: null },
      }),
    );

    fireEvent.change(screen.getByPlaceholderText("Username"), { target: { value: "house" } });
    fireEvent.change(screen.getByPlaceholderText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockSaveCredentials).toHaveBeenCalledWith("fakeprov", { username: "house", password: "pw" }, "shared"));
    expect(mockTestProviderConnection).toHaveBeenCalledWith("fakeprov", "account", "shared");
    expect(mockRevokeAuth).not.toHaveBeenCalled();
  });

  it("removes a login that does not work and says why", async () => {
    mockTestProviderConnection.mockResolvedValue({ app: null, account: { ok: false, detail: "Bad login" } });
    renderCard(
      provider({
        auth_flow: "credentials",
        account_model: "system",
        credential_fields: LOGIN,
        shared: { connected: false, username: null },
      }),
    );

    fireEvent.change(screen.getByPlaceholderText("Username"), { target: { value: "house" } });
    fireEvent.change(screen.getByPlaceholderText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Bad login")).toBeInTheDocument();
    expect(mockRevokeAuth).toHaveBeenCalledWith("fakeprov", "shared");
  });

  it("builds the login form from the fields the provider declares", () => {
    renderCard(
      provider({
        auth_flow: "credentials",
        account_model: "system",
        credential_fields: [{ key: "pin", label: "PIN", secret: true }],
        shared: { connected: false, username: null },
      }),
    );

    expect(screen.getByPlaceholderText("PIN")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Username")).not.toBeInTheDocument();
  });

  it("connects the instance's OAuth2 account as the shared one, once the app is configured", async () => {
    mockGetOAuthAuthorizeUrl.mockResolvedValue("");
    renderCard(
      provider({
        auth_flow: "oauth2",
        account_model: "personal_with_shared",
        client_id: "app",
        personal: OWN,
        shared: { connected: false, username: null },
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: "Connect the instance account" }));

    await waitFor(() => expect(mockGetOAuthAuthorizeUrl).toHaveBeenCalledWith("fakeprov", "shared"));
  });

  it("cannot connect the instance account before the app is set up", () => {
    renderCard(
      provider({ auth_flow: "oauth2", account_model: "system", shared: { connected: false, username: null } }),
    );

    expect(screen.getByRole("button", { name: "Connect the instance account" })).toBeDisabled();
  });

  it("disconnects the SHARED account only", async () => {
    renderCard(
      provider({
        auth_flow: "oauth2",
        account_model: "personal_with_shared",
        client_id: "app",
        personal: OWN,
        shared: { connected: true, username: null },
        authenticated: true,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));

    fireEvent.click(screen.getByRole("button", { name: "Remove account" }));

    await waitFor(() => expect(mockRevokeAuth).toHaveBeenCalledWith("fakeprov", "shared"));
  });

  it("has no instance account for a personal-only provider: the app, and the pointer to Linked accounts", () => {
    renderCard(provider({ auth_flow: "oauth2", account_model: "personal", client_id: "app", personal: OWN, shared: null }));
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));

    expect(screen.queryByText("Instance account")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect the instance account" })).not.toBeInTheDocument();
    expect(screen.getByText("Users connect their own account in Settings › Linked accounts.")).toBeInTheDocument();
  });

  it("tests the instance's API key as the shared account", async () => {
    renderCard(
      provider({
        auth_flow: "api_key",
        account_model: "system",
        shared: { connected: true, username: null },
        authenticated: true,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Configure" }));

    fireEvent.click(screen.getByRole("button", { name: "Test" }));

    await waitFor(() => expect(mockTestProviderConnection).toHaveBeenCalledWith("fakeprov", "account", "shared"));
  });
});

function renderGroup(props: Parameters<typeof ProviderGroup>[0]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <ProviderGroup {...props} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("ProviderGroup (Fase 5c, Task 3/4)", () => {
  it("badges a provider's card with the declared label of the provider_ref setting currently pointing at it", () => {
    const local = provider({ name: "local", label: "Local disk" });

    renderGroup({
      titleKey: "providersTab.storage",
      category: "storage",
      providers: [local],
      // Stand-in for `activeProviderRefBadges`: the frontend never names the
      // role itself — the label comes from the setting's own declaration.
      badgesFor: (name) => (name === "local" ? ["Storage provider"] : []),
    });

    expect(screen.getByText("Storage provider")).toBeInTheDocument();
  });

  it("does not badge a provider that is not the current value of any provider_ref setting", () => {
    const local = provider({ name: "local", label: "Local disk" });

    renderGroup({
      titleKey: "providersTab.storage",
      category: "storage",
      providers: [local],
      badgesFor: () => [],
    });

    expect(screen.queryByText("Storage provider")).not.toBeInTheDocument();
  });

  it("renders a provider genuinely registered in two categories only once, with a compact pointer-back card elsewhere", () => {
    // The backend reports identity explicitly via `also_registered_in`
    // (computed by object identity in routes/config.py::list_providers) —
    // the frontend must trust that field, never re-derive identity from
    // `name` itself. This is the genuine case: one object, two categories.
    const dualInMetadata = provider({
      name: "dualprov",
      label: "Dual Provider",
      also_registered_in: ["sync"],
    });
    const dualInSync = provider({
      name: "dualprov",
      label: "Dual Provider",
      also_registered_in: ["metadata"],
    });

    const { unmount } = renderGroup({
      titleKey: "providersTab.metadata",
      category: "metadata",
      providers: [dualInMetadata],
      badgesFor: () => [],
    });
    // Full editor in the FIRST category in display order.
    expect(screen.getByRole("button", { name: "Configure" })).toBeInTheDocument();
    unmount();

    renderGroup({
      titleKey: "providersTab.sync",
      category: "sync",
      providers: [dualInSync],
      badgesFor: () => [],
    });
    // Compact card in a LATER category: no second editor over the same
    // stored config, just a pointer back to where it lives.
    expect(screen.queryByRole("button", { name: "Configure" })).not.toBeInTheDocument();
    expect(screen.getByText("Configured in the Metadata section.")).toBeInTheDocument();
  });

  it("renders a FULL editor for each of two distinct providers that merely share a name", () => {
    // Regression: the registry can hold two DIFFERENT provider classes that
    // happen to share a `name` (e.g. a metadata provider and
    // a separate download provider, each with its own settings).
    // Neither entry declares the other in `also_registered_in` (that field
    // only lists categories where the SAME object is registered), so both
    // must keep their own full editor — collapsing them by name alone would
    // make the download-only setting unreachable anywhere.
    const metadataShared = provider({ name: "shared", label: "Shared", also_registered_in: [] });
    const downloadShared = provider({ name: "shared", label: "Shared", also_registered_in: [] });

    const { unmount } = renderGroup({
      titleKey: "providersTab.metadata",
      category: "metadata",
      providers: [metadataShared],
      badgesFor: () => [],
    });
    expect(screen.getByRole("button", { name: "Configure" })).toBeInTheDocument();
    expect(screen.queryByText(/Configured in the/)).not.toBeInTheDocument();
    unmount();

    renderGroup({
      titleKey: "providersTab.download",
      category: "download",
      providers: [downloadShared],
      badgesFor: () => [],
    });
    // Its own full editor too — never a compact pointer back to Metadata.
    expect(screen.getByRole("button", { name: "Configure" })).toBeInTheDocument();
    expect(screen.queryByText(/Configured in the/)).not.toBeInTheDocument();
  });
});
