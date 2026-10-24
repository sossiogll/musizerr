import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { RouterProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import { rememberLoginRedirect } from "@/lib/provider-login";
import { ProviderLoginReturnPage } from "./login-provider";

const mockExchange = vi.fn();
const mockStartSession = vi.fn();

vi.mock("@/lib/api-client", () => ({ exchangeLoginCode: (...a: unknown[]) => mockExchange(...a) }));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ startSession: mockStartSession }) }));

const TOKENS = { access_token: "a", refresh_token: "r", token_type: "bearer" };

function httpError(status: number, code: string) {
  const response = { status, data: { detail: { error_code: code, error_message: "x" } }, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
  return new AxiosError("failed", String(status), undefined, undefined, response);
}

function renderReturn(search: string) {
  const root = createRootRoute();
  const page = createRoute({
    getParentRoute: () => root,
    path: "/login/provider",
    component: ProviderLoginReturnPage,
    validateSearch: (s: Record<string, unknown>): { code?: string; error?: string } => ({
      ...(typeof s.code === "string" ? { code: s.code } : {}),
      ...(typeof s.error === "string" ? { error: s.error } : {}),
    }),
  });
  const login = createRoute({ getParentRoute: () => root, path: "/login", component: () => <p>login page</p> });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => <p>home page</p> });
  const album = createRoute({ getParentRoute: () => root, path: "/album/$provider/$id", component: () => <p>album page</p> });
  const router = createRouter({
    routeTree: root.addChildren([page, login, home, album]),
    history: createMemoryHistory({ initialEntries: [`/login/provider${search}`] }),
  });
  return render(
    <I18nProvider>
      <RouterProvider router={router} />
    </I18nProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem("musizeer-lang", "en");
  sessionStorage.clear();
  mockExchange.mockReset().mockResolvedValue(TOKENS);
  mockStartSession.mockReset().mockResolvedValue(undefined);
});

describe("The page an authorization at a provider comes back to", () => {
  it("exchanges the one-time code once, starts the session and goes home", async () => {
    renderReturn("?code=abc");

    expect(await screen.findByText("home page")).toBeInTheDocument();
    expect(mockExchange).toHaveBeenCalledTimes(1);
    expect(mockExchange).toHaveBeenCalledWith("abc");
    expect(mockStartSession).toHaveBeenCalledWith(TOKENS);
  });

  it("goes where the viewer was going before leaving, and forgets it", async () => {
    rememberLoginRedirect("/album/p/1");

    renderReturn("?code=abc");

    expect(await screen.findByText("album page")).toBeInTheDocument();
    expect(sessionStorage.getItem("musizeer.login-redirect")).toBeNull();
  });

  it("never follows a remembered target that leaves the app", async () => {
    sessionStorage.setItem("musizeer.login-redirect", "https://evil.example/");

    renderReturn("?code=abc");

    expect(await screen.findByText("home page")).toBeInTheDocument();
  });

  it("a code that no longer works is explained", async () => {
    mockExchange.mockRejectedValue(httpError(401, "provider_login_invalid_code"));

    renderReturn("?code=abc");

    expect(await screen.findByRole("alert")).toHaveTextContent("This sign-in link was already used or has expired");
    expect(mockStartSession).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Back to login" })).toBeInTheDocument();
  });

  it("an error code in the address is explained and nothing is exchanged", async () => {
    renderReturn("?error=provider_login_disabled");

    expect(await screen.findByRole("alert")).toHaveTextContent("New accounts from a provider are not allowed on this server.");
    expect(screen.getByText("Sign-in failed")).toBeInTheDocument();
    expect(mockExchange).not.toHaveBeenCalled();
  });

  it("a new account waiting for an administrator says so, not that the sign-in failed", async () => {
    renderReturn("?error=account_pending");

    expect(await screen.findByText("Waiting for approval")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("waiting for an administrator to activate it");
  });

  it("an unknown error code gets a generic message", async () => {
    renderReturn("?error=something_new");

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not sign you in. Try again."));
  });
});
