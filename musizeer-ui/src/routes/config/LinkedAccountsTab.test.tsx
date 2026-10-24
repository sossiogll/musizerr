import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { I18nProvider } from "@/i18n";
import { LinkedAccountsTab } from "./LinkedAccountsTab";
import type { LinkedAccount } from "@/types";

const mockGetLinkedAccounts = vi.fn();
const mockGetOAuthAuthorizeUrl = vi.fn();
const mockRevokeAuth = vi.fn();
const mockSaveCredentials = vi.fn();
const mockTestProviderConnection = vi.fn();

vi.mock("@/lib/api-client", () => ({
  getLinkedAccounts: (...a: unknown[]) => mockGetLinkedAccounts(...a),
  getOAuthAuthorizeUrl: (...a: unknown[]) => mockGetOAuthAuthorizeUrl(...a),
  revokeAuth: (...a: unknown[]) => mockRevokeAuth(...a),
  saveCredentials: (...a: unknown[]) => mockSaveCredentials(...a),
  testProviderConnection: (...a: unknown[]) => mockTestProviderConnection(...a),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const LOGIN = [
  { key: "username", label: "Username", secret: false },
  { key: "password", label: "Password", secret: true },
];

// GET /auth/linked-accounts: the entries as the backend serializes them
// (services/linked_accounts.py::linked_accounts).
function account(overrides: Partial<LinkedAccount> = {}): LinkedAccount {
  return {
    name: "origin",
    label: "Origin",
    auth_flow: "oauth2",
    account_model: "personal_with_shared",
    connectable: true,
    credential_fields: [],
    personal: { connected: false, username: null, missing_permissions: [], capabilities: ["user_library", "unlike_saved_track"], login: null },
    shared: { connected: false, username: null },
    ...overrides,
  };
}

function connected(overrides: Partial<LinkedAccount["personal"]> = {}): LinkedAccount["personal"] {
  return { connected: true, username: null, missing_permissions: [], capabilities: ["user_library"], login: null, ...overrides };
}

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <LinkedAccountsTab />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("musizeer-lang", "en");
  mockGetLinkedAccounts.mockReset().mockResolvedValue([account()]);
  mockGetOAuthAuthorizeUrl.mockReset().mockResolvedValue("");
  mockRevokeAuth.mockReset().mockResolvedValue({ ok: true });
  mockSaveCredentials.mockReset().mockResolvedValue({ success: true });
  mockTestProviderConnection.mockReset().mockResolvedValue({ app: null, account: { ok: true, detail: "Connected" } });
});

describe("Settings › Linked accounts", () => {
  it("lists what the API says, with what each connection enables", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account(), account({ name: "second", label: "Second" })]);

    renderTab();

    expect(await screen.findByText("Origin")).toBeInTheDocument();
    expect(screen.getByText("Second")).toBeInTheDocument();
    expect(screen.getAllByText(/Enables: import your likes, take a like back/)).toHaveLength(2);
  });

  it("says so when no provider has personal accounts", async () => {
    mockGetLinkedAccounts.mockResolvedValue([]);

    renderTab();

    expect(await screen.findByText("No provider lets you connect an account of your own.")).toBeInTheDocument();
  });

  it("asks for the viewer's OWN account when connecting an OAuth2 provider", async () => {
    renderTab();

    fireEvent.click(await screen.findByRole("button", { name: "Connect" }));

    await waitFor(() => expect(mockGetOAuthAuthorizeUrl).toHaveBeenCalledWith("origin", "personal"));
  });

  it("cannot connect before an administrator has set the service up", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ connectable: false })]);

    renderTab();

    expect(await screen.findByRole("button", { name: "Connect" })).toBeDisabled();
    expect(screen.getByText("An administrator has to set this service up before accounts can be connected.")).toBeInTheDocument();
  });

  it("without an own account, says the instance's account serves them", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ shared: { connected: true, username: null } })]);

    renderTab();

    expect(await screen.findByText("Using the instance account")).toBeInTheDocument();
    expect(screen.getByText(/the instance's account is used, and what needs your own account/)).toBeInTheDocument();
  });

  it("without an own account and without an instance one, says it is not available", async () => {
    renderTab();

    expect(await screen.findByText(/the instance has none: this service is not available to you/)).toBeInTheDocument();
  });

  it("a personal-only provider does not mention an instance account", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ account_model: "personal", shared: null })]);

    renderTab();

    await screen.findByText("Origin");
    expect(screen.queryByText(/instance/)).not.toBeInTheDocument();
  });

  it("asks to reconnect an account connected without a permission, by capability", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ personal: connected({ missing_permissions: ["unlike_saved_track"] }) })]);

    renderTab();

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Reconnect to allow removing likes: this account was connected before that permission was asked for.",
    );
    expect(screen.getByRole("button", { name: "Reconnect" })).toBeInTheDocument();
  });

  it("says nothing of permissions when the account has them all", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ personal: connected() })]);

    renderTab();

    expect(await screen.findByRole("button", { name: "Change account" })).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("disconnects only the viewer's own account", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ personal: connected() })]);

    renderTab();

    fireEvent.click(await screen.findByRole("button", { name: "Disconnect" }));

    await waitFor(() => expect(mockRevokeAuth).toHaveBeenCalledWith("origin", "personal"));
  });

  it("tests the viewer's own account", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ personal: connected() })]);

    renderTab();

    fireEvent.click(await screen.findByRole("button", { name: "Test" }));

    await waitFor(() => expect(mockTestProviderConnection).toHaveBeenCalledWith("origin", "account", "personal"));
  });

  describe("a login account", () => {
    const login = (overrides: Partial<LinkedAccount> = {}) =>
      account({ name: "login", label: "Login", auth_flow: "credentials", credential_fields: LOGIN, ...overrides });

    it("is a form of the fields the provider declares, saved as the own account and tested", async () => {
      mockGetLinkedAccounts.mockResolvedValue([login()]);

      renderTab();

      fireEvent.change(await screen.findByPlaceholderText("Username"), { target: { value: "me" } });
      fireEvent.change(screen.getByPlaceholderText("Password"), { target: { value: "pw" } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      await waitFor(() => expect(mockSaveCredentials).toHaveBeenCalledWith("login", { username: "me", password: "pw" }, "personal"));
      expect(mockTestProviderConnection).toHaveBeenCalledWith("login", "account", "personal");
      expect(mockRevokeAuth).not.toHaveBeenCalled();
    });

    it("does not keep a login that fails its test, and says why", async () => {
      mockGetLinkedAccounts.mockResolvedValue([login()]);
      mockTestProviderConnection.mockResolvedValue({ app: null, account: { ok: false, detail: "Wrong password" } });

      renderTab();

      fireEvent.change(await screen.findByPlaceholderText("Username"), { target: { value: "me" } });
      fireEvent.change(screen.getByPlaceholderText("Password"), { target: { value: "pw" } });
      fireEvent.click(screen.getByRole("button", { name: "Save" }));

      expect(await screen.findByText("Wrong password")).toBeInTheDocument();
      expect(mockRevokeAuth).toHaveBeenCalledWith("login", "personal");
    });

    it("shows who is connected and never asks again until the viewer wants to change it", async () => {
      mockGetLinkedAccounts.mockResolvedValue([login({ personal: connected({ username: "me" }) })]);

      renderTab();

      expect(await screen.findByText("Connected as me")).toBeInTheDocument();
      expect(screen.queryByPlaceholderText("Username")).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Change account" }));
      expect(screen.getByPlaceholderText("Username")).toBeInTheDocument();
    });
  });
});

describe("Settings › Linked accounts — sign-in through an account", () => {
  it("says an account that can sign the viewer in does", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ personal: connected({ capabilities: ["login"], login: true }) })]);

    renderTab();

    expect(await screen.findByText("You can sign in with this account.")).toBeInTheDocument();
    expect(screen.getByText(/Enables: sign in to Musizeer/)).toBeInTheDocument();
  });

  it("asks to reconnect an account that was linked before sign-in existed", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ personal: connected({ capabilities: ["login"], login: false }) })]);

    renderTab();

    expect(await screen.findByRole("status")).toHaveTextContent("Reconnect this account to be able to sign in with it.");
  });

  it("says nothing about signing in for a provider that cannot sign anybody in", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ personal: connected() })]);

    renderTab();

    await screen.findByText("Origin");
    expect(screen.queryByText(/sign in/i)).not.toBeInTheDocument();
  });

  it("says so when the account is already linked to another user", async () => {
    mockGetLinkedAccounts.mockResolvedValue([account({ auth_flow: "credentials", credential_fields: LOGIN })]);
    const conflict = new AxiosError("failed", "409", undefined, undefined, { status: 409, data: { detail: "x" }, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse);
    mockSaveCredentials.mockRejectedValue(conflict);

    renderTab();
    fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("This account is already linked to another user.")).toBeInTheDocument();
  });
});
