import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import { LoginPage, loginErrorKey } from "./login";

const mockLogin = vi.fn();
const mockGetLoginProviders = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getLoginProviders: () => mockGetLoginProviders(),
}));
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ login: mockLogin, authConfig: { registration_open: false } }),
}));

function httpError(status: number) {
  const response = { status, data: { detail: "x" }, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
  return new AxiosError("failed", String(status), undefined, undefined, response);
}

describe("loginErrorKey", () => {
  it.each([
    [httpError(401), "auth.invalidCredentials"],
    [httpError(403), "auth.accountDeactivated"],
    [httpError(400), "auth.authDisabled"],
    [httpError(500), "auth.serverUnavailable"],
    [httpError(503), "auth.serverUnavailable"],
    [new AxiosError("Network Error", "ERR_NETWORK"), "auth.serverUnavailable"],
    [new AxiosError("timeout", "ECONNABORTED"), "auth.serverUnavailable"],
  ])("%s → %s", (error, key) => {
    expect(loginErrorKey(error)).toBe(key);
  });
});

function renderLogin(entry = "/login") {
  const root = createRootRoute();
  const login = createRoute({ getParentRoute: () => root, path: "/login", component: LoginPage });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => <p>home</p> });
  const album = createRoute({
    getParentRoute: () => root,
    path: "/album/$provider/$id",
    component: function Album() {
      const { id } = album.useParams();
      const search = album.useSearch() as { tab?: string };
      return <p>album {id} {search.tab}</p>;
    },
  });
  const router = createRouter({
    routeTree: root.addChildren([login, home, album]),
    history: createMemoryHistory({ initialEntries: [entry] }),
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <RouterProvider router={router} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => mockGetLoginProviders.mockReset().mockResolvedValue([]));

async function submit() {
  fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "alice" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw" } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("LoginPage", () => {
  it("says the credentials are wrong on a 401", async () => {
    mockLogin.mockRejectedValue(httpError(401));
    renderLogin();
    await submit();
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password.");
  });

  it("says the server is unavailable on a 500 or no answer — not 'wrong password'", async () => {
    mockLogin.mockRejectedValue(httpError(500));
    renderLogin();
    await submit();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The server is not available right now.");
    expect(alert).not.toHaveTextContent("Invalid");
  });

  it("goes back to the page the viewer came from, with its search", async () => {
    mockLogin.mockResolvedValue(undefined);
    renderLogin(`/login?redirect=${encodeURIComponent("/album/p/42?tab=tracks")}`);
    await submit();
    expect(await screen.findByText("album 42 tracks")).toBeInTheDocument();
  });

  it.each(["https://evil.example/phish", "//evil.example/x", "javascript:alert(1)", "/login"])(
    "never follows %s: Home instead",
    async (target) => {
      mockLogin.mockResolvedValue(undefined);
      renderLogin(`/login?redirect=${encodeURIComponent(target)}`);
      await submit();
      expect(await screen.findByText("home")).toBeInTheDocument();
    },
  );
});

describe("LoginPage — sign-in through a provider", () => {
  it("shows no provider button when the server lists none", async () => {
    renderLogin();

    await screen.findByLabelText("Username");
    expect(screen.queryByRole("button", { name: /Continue with/ })).not.toBeInTheDocument();
  });

  it("lists the providers the server says can sign users in, below the form", async () => {
    mockGetLoginProviders.mockResolvedValue([
      { name: "orbit", label: "Orbit", auth_flow: "oauth2", credential_fields: [] },
      { name: "gate", label: "Gate", auth_flow: "credentials", credential_fields: [{ key: "username", label: "Name", secret: false }] },
    ]);

    renderLogin();

    expect(await screen.findByRole("button", { name: "Continue with Orbit" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with Gate" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });
});
