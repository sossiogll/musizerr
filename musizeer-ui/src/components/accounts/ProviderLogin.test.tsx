import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import type { LoginProvider } from "@/types";
import { ProviderLogin } from "./ProviderLogin";

const mockGetLoginProviders = vi.fn();
const mockLoginWithCredentials = vi.fn();
const mockStartSession = vi.fn();
const mockStart = vi.fn();

vi.mock("@/lib/api-client", () => ({
  getLoginProviders: () => mockGetLoginProviders(),
  loginWithProviderCredentials: (...a: unknown[]) => mockLoginWithCredentials(...a),
}));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ startSession: mockStartSession }) }));
vi.mock("@/lib/provider-login", async () => ({
  ...(await vi.importActual<typeof import("@/lib/provider-login")>("@/lib/provider-login")),
  startProviderLogin: (...a: unknown[]) => mockStart(...a),
}));

const FIELDS = [
  { key: "username", label: "Account name", secret: false },
  { key: "pin", label: "PIN", secret: true },
];
const OAUTH: LoginProvider = { name: "orbit", label: "Orbit", auth_flow: "oauth2", credential_fields: [] };
const FORM: LoginProvider = { name: "gate", label: "Gate", auth_flow: "credentials", credential_fields: FIELDS };

function httpError(status: number, detail: unknown) {
  const response = { status, data: { detail }, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
  return new AxiosError("failed", String(status), undefined, undefined, response);
}

function renderLogin(redirect?: string) {
  const root = createRootRoute();
  const page = createRoute({ getParentRoute: () => root, path: "/login", component: () => <ProviderLogin redirect={redirect} /> });
  const target = createRoute({ getParentRoute: () => root, path: "/album/$provider/$id", component: () => <p>album page</p> });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => <p>home page</p> });
  const router = createRouter({ routeTree: root.addChildren([page, target, home]), history: createMemoryHistory({ initialEntries: ["/login"] }) });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <RouterProvider router={router} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("musizeer-lang", "en");
  mockGetLoginProviders.mockReset().mockResolvedValue([OAUTH, FORM]);
  mockLoginWithCredentials.mockReset().mockResolvedValue({ access_token: "a", refresh_token: "r", token_type: "bearer" });
  mockStartSession.mockReset().mockResolvedValue(undefined);
  mockStart.mockReset();
});

describe("ProviderLogin", () => {
  it("renders nothing when no provider can sign users in", async () => {
    mockGetLoginProviders.mockResolvedValue([]);

    const { container } = renderLogin();

    await waitFor(() => expect(mockGetLoginProviders).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("offers one button per provider, worded from the label the API gives", async () => {
    renderLogin();

    expect(await screen.findByRole("button", { name: "Continue with Orbit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with Gate" })).toBeInTheDocument();
  });

  it("an OAuth2 provider sends the browser to the server, remembering where the viewer was going", async () => {
    renderLogin("/album/p/1");

    fireEvent.click(await screen.findByRole("button", { name: "Continue with Orbit" }));

    expect(mockStart).toHaveBeenCalledWith("orbit", "/album/p/1");
  });

  it("a provider with a login account opens the form it declares, field by field", async () => {
    renderLogin();

    fireEvent.click(await screen.findByRole("button", { name: "Continue with Gate" }));

    expect(screen.getByText("Sign in with your Gate account.")).toBeInTheDocument();
    const name = screen.getByLabelText("Account name");
    expect(name).toHaveAttribute("type", "text");
    expect(screen.getByLabelText("PIN")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
  });

  it("posts the form to that provider, starts the session and goes where the viewer was going", async () => {
    renderLogin("/album/p/1");

    fireEvent.click(await screen.findByRole("button", { name: "Continue with Gate" }));
    fireEvent.change(screen.getByLabelText("Account name"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("PIN"), { target: { value: "1234" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(mockLoginWithCredentials).toHaveBeenCalledWith("gate", { username: "alice", pin: "1234" }));
    await waitFor(() => expect(mockStartSession).toHaveBeenCalledWith({ access_token: "a", refresh_token: "r", token_type: "bearer" }));
    expect(await screen.findByText("album page")).toBeInTheDocument();
  });

  it.each([
    [403, "provider_login_disabled", /No account is linked to this one/],
    [403, "account_pending", /waiting for an administrator to activate it/],
    [401, "provider_login_refused", /did not accept this sign-in/],
    [429, "provider_login_rate_limited", /Too many attempts/],
  ])("a %s with the code %s is explained, not shown as a generic failure", async (status, code, text) => {
    mockLoginWithCredentials.mockRejectedValue(httpError(status, { error_code: code, error_message: "x" }));
    renderLogin();

    fireEvent.click(await screen.findByRole("button", { name: "Continue with Gate" }));
    fireEvent.change(screen.getByLabelText("Account name"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("PIN"), { target: { value: "1234" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(mockStartSession).not.toHaveBeenCalled();
  });

  it("an unreachable server is said to be unavailable", async () => {
    mockLoginWithCredentials.mockRejectedValue(new AxiosError("Network Error", "ERR_NETWORK"));
    renderLogin();

    fireEvent.click(await screen.findByRole("button", { name: "Continue with Gate" }));
    fireEvent.change(screen.getByLabelText("Account name"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("PIN"), { target: { value: "1234" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByText(/The provider is not available right now/)).toBeInTheDocument();
  });
});
