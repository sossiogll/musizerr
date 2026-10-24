import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import { I18nProvider } from "@/i18n";
import { SettingsArea } from "./SettingsArea";
import type { SettingDef, ConfigSchemaResponse } from "@/lib/api-client";
import type { ProviderInfo, ProvidersResponse } from "@/types";

const mockGetSettingsSchema = vi.fn();
const mockGetSettingsValues = vi.fn();
const mockGetProviders = vi.fn();
const mockPatchSettings = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client");
  return {
    ...actual,
    getSettingsSchema: (...args: unknown[]) => mockGetSettingsSchema(...args),
    getSettingsValues: (...args: unknown[]) => mockGetSettingsValues(...args),
    getProviders: (...args: unknown[]) => mockGetProviders(...args),
    patchSettings: (...args: unknown[]) => mockPatchSettings(...args),
  };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

function def(overrides: Partial<SettingDef> & Pick<SettingDef, "key" | "type">): SettingDef {
  return {
    group: "g1",
    area: "test",
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

function provider(name: string, label: string): ProviderInfo {
  return {
    name,
    label,
    auth_flow: "none" as ProviderInfo["auth_flow"],
    account_model: "none",
    personal: null,
    shared: null,
    authenticated: true,
    usable: true,
    config: {},
  };
}

function providersResponse(overrides: Partial<ProvidersResponse> = {}): ProvidersResponse {
  return { metadata: [], download: [], lyrics: [], storage: [], sync: [], identification: [], ...overrides };
}

function taxonomy(groups: { key: string; label: string; description?: string }[]): ConfigSchemaResponse["taxonomy"] {
  return {
    areas: [{ key: "test", label_key: "settings.areas.test", label: "Test area" }],
    groups: groups.map((g) => ({ key: g.key, label_key: `settings.groups.${g.key}`, label: g.label, description_key: `settings.group_descriptions.${g.key}`, description: g.description ?? "", area: "test" })),
  };
}

function renderArea() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <SettingsArea area="test" />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockGetSettingsSchema.mockReset();
  mockGetSettingsValues.mockReset();
  mockGetProviders.mockReset();
  mockPatchSettings.mockReset();
  mockGetProviders.mockResolvedValue(providersResponse());
  vi.mocked(toast.success).mockReset();
  vi.mocked(toast.error).mockReset();
});

describe("SettingsArea", () => {
  it("renders a control matching each declared setting type", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [
        def({ key: "flag", type: "bool", label: "Flag" }),
        def({ key: "mode", type: "enum", label: "Mode", options: ["a", "b"] }),
        def({ key: "count", type: "int", label: "Count", minimum: 0, maximum: 10, step: 1 }),
        def({ key: "ratio", type: "float", label: "Ratio", minimum: 0, maximum: 1, step: 0.1 }),
        def({ key: "name", type: "string", label: "Name" }),
        def({ key: "token", type: "string", label: "Token", secret: true, required: false }),
        def({ key: "storage_provider", type: "provider_ref", label: "Storage", provider_category: "storage" }),
        def({
          key: "download_provider_order",
          type: "list",
          label: "Download order",
          provider_category: "download",
        }),
      ],
      taxonomy: taxonomy([{ key: "g1", label: "Group One", description: "A group" }]),
    });
    mockGetSettingsValues.mockResolvedValue({
      flag: true,
      mode: "b",
      count: 5,
      ratio: 0.5,
      name: "hello",
      token: "",
      storage_provider: "local",
      download_provider_order: ["b", "a"],
    });
    mockGetProviders.mockResolvedValue(
      providersResponse({
        storage: [provider("local", "Local disk")],
        download: [provider("a", "Provider A"), provider("b", "Provider B")],
      }),
    );

    renderArea();

    await screen.findByText("Group One");
    expect(screen.getByText("A group")).toBeInTheDocument();

    // bool -> switch, checked
    expect(screen.getByRole("switch")).toHaveAttribute("data-state", "checked");
    // enum -> select with the current option
    expect(screen.getByDisplayValue("b")).toBeInTheDocument();
    // int / float -> number inputs
    expect(screen.getByDisplayValue("5")).toBeInTheDocument();
    expect(screen.getByDisplayValue("0.5")).toBeInTheDocument();
    // string -> text input
    expect(screen.getByDisplayValue("hello")).toBeInTheDocument();
    // secret string -> password input
    const passwordInputs = document.querySelectorAll('input[type="password"]');
    expect(passwordInputs.length).toBe(1);
    // provider_ref -> select labelled with the provider's own label, not its name
    expect(screen.getByDisplayValue("Local disk")).toBeInTheDocument();
    // list -> reorderable list, sourced from GET /providers, labelled likewise
    expect(screen.getByText("Provider A")).toBeInTheDocument();
    expect(screen.getByText("Provider B")).toBeInTheDocument();
  });

  it("never renders a provider-namespaced setting — that is the provider card's job", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [
        def({ key: "download_provider_order", type: "list", label: "Download order", group: "provider_selection", provider_category: "download" }),
        // Shape alone marks this as belonging to a provider's own card
        // (ProvidersTab), never to the generic area renderer — regardless of
        // which provider happens to declare it (no provider name is special-cased).
        def({ key: "provider.somehost.search_limit", type: "int", label: "Search limit", group: "somehost" }),
      ],
      taxonomy: taxonomy([{ key: "provider_selection", label: "Provider selection" }, { key: "somehost", label: "Somehost" }]),
    });
    mockGetSettingsValues.mockResolvedValue({ download_provider_order: [], "provider.somehost.search_limit": 25 });
    mockGetProviders.mockResolvedValue(
      providersResponse({ download: [provider("a", "Provider A"), provider("b", "Provider B")] }),
    );

    renderArea();

    await screen.findByText("Provider A");
    expect(screen.queryByText("Search limit")).not.toBeInTheDocument();
    expect(screen.queryByText("Somehost")).not.toBeInTheDocument();
  });

  it("hides a setting whose visible_when is unmet, and reveals it immediately on an unsaved edit", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [
        def({ key: "enableAdvanced", type: "bool", label: "Enable advanced" }),
        def({
          key: "advancedOption",
          type: "string",
          label: "Advanced option",
          visible_when: [["enableAdvanced", true]],
        }),
      ],
      taxonomy: taxonomy([{ key: "g1", label: "Group One" }]),
    });
    mockGetSettingsValues.mockResolvedValue({ enableAdvanced: false });

    renderArea();

    await screen.findByText("Group One");
    expect(screen.queryByText("Advanced option")).not.toBeInTheDocument();

    // Toggling the switch is an UNSAVED edit — no save happened, no network
    // call was made — yet the dependent setting must appear right away.
    fireEvent.click(screen.getByRole("switch"));

    await waitFor(() => expect(screen.getByText("Advanced option")).toBeInTheDocument());
    expect(mockPatchSettings).not.toHaveBeenCalled();
  });

  it("batches every edit into a single PATCH /settings request", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [
        def({ key: "flag", type: "bool", label: "Flag" }),
        def({ key: "name", type: "string", label: "Name" }),
      ],
      taxonomy: taxonomy([{ key: "g1", label: "Group One" }]),
    });
    mockGetSettingsValues.mockResolvedValue({ flag: false, name: "old" });
    mockPatchSettings.mockResolvedValue({ flag: true, name: "new" });

    renderArea();

    await screen.findByText("Group One");
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.change(screen.getByDisplayValue("old"), { target: { value: "new" } });

    // One combined save action for both edits, via the sticky bar.
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledTimes(1));
    expect(mockPatchSettings).toHaveBeenCalledWith({ flag: true, name: "new" });
  });

  it("surfaces the backend's rejection detail when the batch is refused", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [def({ key: "count", type: "int", label: "Count", minimum: 0, maximum: 10 })],
      taxonomy: taxonomy([{ key: "g1", label: "Group One" }]),
    });
    mockGetSettingsValues.mockResolvedValue({ count: 1 });
    mockPatchSettings.mockRejectedValue({
      message: "Request failed",
      response: { data: { detail: "count: must be <= 10" } },
    });

    renderArea();

    await screen.findByText("Group One");
    fireEvent.change(screen.getByDisplayValue("1"), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("count: must be <= 10"));
    // Rejected as a whole: the edit must stay live (not silently cleared),
    // so the user can see and fix what was refused.
    expect(screen.getByDisplayValue("20")).toBeInTheDocument();
  });

  it("shows a refusal that carries an error code in the user's language", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [def({ key: "auth_enabled", type: "bool", label: "Authentication" })],
      taxonomy: taxonomy([{ key: "g1", label: "Group One" }]),
    });
    mockGetSettingsValues.mockResolvedValue({ auth_enabled: false });
    mockPatchSettings.mockRejectedValue({
      message: "Request failed",
      response: {
        data: { detail: { error_code: "auth_secret_insecure", error_message: "auth_enabled: MUSIZEER_AUTH_SECRET is too short" } },
      },
    });

    renderArea();

    await screen.findByText("Group One");
    fireEvent.click(screen.getByRole("switch"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(expect.stringContaining("Authentication can't be turned on")),
    );
  });

  it("renders an unavailable setting disabled, with its reason shown next to it", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [
        def({
          key: "identification_enabled",
          type: "bool",
          label: "Audio identification",
          available: false,
          unavailable_reason: "requires a usable 'identification' provider",
        }),
      ],
      taxonomy: taxonomy([{ key: "g1", label: "Group One" }]),
    });
    // Already off — nothing to switch back, so fully locked, not just
    // styled to look that way.
    mockGetSettingsValues.mockResolvedValue({ identification_enabled: false });

    renderArea();

    await screen.findByText("requires a usable 'identification' provider");
    expect(screen.getByRole("switch")).toBeDisabled();
  });

  it("skips a hidden setting entirely, and never renders a group left with none visible", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [
        def({ key: "visible", type: "bool", label: "Visible flag", group: "g1" }),
        // Has its own dedicated page (e.g. the system routines) — the
        // generic settings UI must skip it, not just disable it.
        def({ key: "routine.sync_saved_tracks.enabled", type: "bool", label: "Routine enabled", group: "routines", hidden: true }),
        def({ key: "routine.sync_saved_tracks.interval_minutes", type: "int", label: "Routine interval", group: "routines", hidden: true }),
      ],
      taxonomy: taxonomy([
        { key: "g1", label: "Group One" },
        { key: "routines", label: "Routines group" },
      ]),
    });
    mockGetSettingsValues.mockResolvedValue({
      visible: true,
      "routine.sync_saved_tracks.enabled": true,
      "routine.sync_saved_tracks.interval_minutes": 60,
    });

    renderArea();

    await screen.findByText("Group One");
    expect(screen.queryByText("Routine enabled")).not.toBeInTheDocument();
    expect(screen.queryByText("Routine interval")).not.toBeInTheDocument();
    // The group made up entirely of hidden settings must not render an
    // empty card at all.
    expect(screen.queryByText("Routines group")).not.toBeInTheDocument();
  });

  it("still lets a non-default value on an unavailable setting be switched back to its default", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      settings: [
        def({
          key: "identification_enabled",
          type: "bool",
          label: "Audio identification",
          available: false,
          unavailable_reason: "requires a usable 'identification' provider",
        }),
      ],
      taxonomy: taxonomy([{ key: "g1", label: "Group One" }]),
    });
    // Stuck ON while its requirement is unmet (e.g. the provider that used to
    // satisfy it was removed) — must still appear on, with the reason, and
    // remain switchable off.
    mockGetSettingsValues.mockResolvedValue({ identification_enabled: true });
    mockPatchSettings.mockResolvedValue({ identification_enabled: false });

    renderArea();

    await screen.findByText("requires a usable 'identification' provider");
    const toggle = screen.getByRole("switch");
    expect(toggle).toHaveAttribute("data-state", "checked");
    expect(toggle).not.toBeDisabled();

    // Switching it off is the ONLY direction the server accepts anyway (it
    // equals the declared default) — the batched save bar still applies,
    // same as any other bool setting.
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mockPatchSettings).toHaveBeenCalledWith({ identification_enabled: false }),
    );
  });
});
