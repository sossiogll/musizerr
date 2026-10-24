import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/i18n";
import { ProviderRefControl } from "./ProviderRefControl";
import type { SettingDef } from "@/lib/api-client";
import type { ProviderInfo, ProvidersResponse } from "@/types";

const mockPatchSettings = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client");
  return {
    ...actual,
    patchSettings: (...args: unknown[]) => mockPatchSettings(...args),
  };
});

function def(overrides: Partial<SettingDef> & Pick<SettingDef, "key">): SettingDef {
  return {
    type: "provider_ref",
    group: "provider_selection",
    area: "providers",
    label: "Sync provider",
    description: "",
    admin_only: false,
    options: null,
    minimum: null,
    maximum: null,
    step: null,
    secret: false,
    provider_category: "sync",
    required: false,
    visible_when: null,
    available: true,
    unavailable_reason: null,
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

function renderControl(setting: SettingDef, value: unknown, providers: ProvidersResponse) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <ProviderRefControl def={setting} value={value} providers={providers} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockPatchSettings.mockReset();
});

describe("ProviderRefControl", () => {
  it("only sends the change via the explicit activate action, one key at a time, with an in-progress state", async () => {
    let resolvePatch!: (v: unknown) => void;
    mockPatchSettings.mockReturnValue(
      new Promise((resolve) => {
        resolvePatch = resolve;
      }),
    );

    const providers = providersResponse({ sync: [provider("libsrv", "Library server"), provider("other", "Other sync")] });
    renderControl(def({ key: "sync_provider" }), "libsrv", providers);

    // Picking a candidate from the menu is not itself a request.
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "other" } });
    expect(mockPatchSettings).not.toHaveBeenCalled();

    // The explicit action sends exactly this one key. React Query invokes
    // the mutation function on a microtask, so this needs an async wait
    // rather than a synchronous assertion right after the click.
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));
    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledTimes(1));
    expect(mockPatchSettings).toHaveBeenCalledWith({ sync_provider: "other" });

    await screen.findByText("Testing…");

    resolvePatch({ sync_provider: "other" });
    await waitFor(() => expect(screen.queryByText("Testing…")).not.toBeInTheDocument());
  });

  it("keeps the previous active provider selected and shows the provider's error inline on rejection", async () => {
    mockPatchSettings.mockRejectedValue({
      message: "Request failed",
      response: { data: { detail: "connection test failed for 'other': timed out" } },
    });

    const providers = providersResponse({ sync: [provider("libsrv", "Library server"), provider("other", "Other sync")] });
    renderControl(def({ key: "sync_provider" }), "libsrv", providers);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "other" } });
    fireEvent.click(screen.getByRole("button", { name: "Activate" }));

    await waitFor(() =>
      expect(screen.getByText("connection test failed for 'other': timed out")).toBeInTheDocument(),
    );
    // Reverted to the still-active provider, never the failed candidate.
    expect(screen.getByDisplayValue("Library server")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Activate" })).not.toBeInTheDocument();
  });

  it("clears an optional reference immediately, with no explicit action and no test", async () => {
    mockPatchSettings.mockResolvedValue({ sync_provider: null });
    const providers = providersResponse({ sync: [provider("libsrv", "Library server")] });
    renderControl(def({ key: "sync_provider", required: false }), "libsrv", providers);

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "" } });

    await waitFor(() => expect(mockPatchSettings).toHaveBeenCalledWith({ sync_provider: "" }));
    expect(screen.queryByRole("button", { name: "Activate" })).not.toBeInTheDocument();
  });

  it("disables the control when unavailable and already cleared (nothing to switch off)", () => {
    const providers = providersResponse({ sync: [provider("libsrv", "Library server")] });
    renderControl(
      def({
        key: "sync_provider",
        available: false,
        unavailable_reason: "requires a usable 'sync' provider",
      }),
      null,
      providers,
    );

    expect(screen.getByRole("combobox")).toBeDisabled();
  });
});
