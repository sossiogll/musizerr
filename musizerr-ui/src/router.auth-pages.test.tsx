import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Outlet, RouterProvider, createMemoryHistory, createRouter } from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import { viewerStatus } from "@/test/viewer";
import type { AuthConfig, ViewerMode } from "@/types";

// The real route tree, with the app shell and the pages other than the auth ones stood in: what is
// under test is which of the sign-in, register and provider-return addresses a viewer can reach.
let status: AuthConfig;
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ authConfig: status, login: vi.fn(), startSession: vi.fn(), user: null }),
}));
vi.mock("./routes/root", () => ({ RootLayout: () => <Outlet /> }));
vi.mock("./routes/home", () => ({ HomePage: () => <p>home page</p> }));
vi.mock("./routes/playlists", () => ({ PlaylistsPage: () => <p>playlists page</p> }));
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getLoginProviders: async () => [],
  exchangeLoginCode: async () => ({ access_token: "a", refresh_token: "r", token_type: "bearer" }),
}));

async function visit(mode: ViewerMode, entry: string, overrides: Partial<AuthConfig> = {}) {
  status = { ...viewerStatus(mode), ...overrides };
  const { router } = await import("./router");
  const testRouter = createRouter({ routeTree: router.routeTree, history: createMemoryHistory({ initialEntries: [entry] }) });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider>
        <RouterProvider router={testRouter} />
      </I18nProvider>
    </QueryClientProvider>,
  );
  return testRouter;
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("the auth pages with authentication off", () => {
  it.each(["/login", "/register", "/login/provider?code=abc", "/login/provider?error=account_pending"])(
    "%s sends the visitor to Home",
    async (entry) => {
      const router = await visit("open", entry);

      expect(await screen.findByText("home page")).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/");
    },
  );

  it("sends the visitor back to where they came from", async () => {
    const router = await visit("open", "/login?redirect=%2Fplaylists");

    expect(await screen.findByText("playlists page")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/playlists");
  });

  it.each(["https://evil.example/", "//evil.example", "/login", "javascript:alert(1)"])(
    "never leaves the app: a redirect to %s means Home",
    async (target) => {
      const router = await visit("open", `/login?redirect=${encodeURIComponent(target)}`);

      expect(await screen.findByText("home page")).toBeInTheDocument();
      expect(router.state.location.pathname).toBe("/");
    },
  );

  it("replaces the page in the history: Back does not return to the sign-in", async () => {
    const router = await visit("open", "/login");
    await screen.findByText("home page");

    expect(router.history.location.pathname).toBe("/");
    expect(router.history.length).toBe(1);
  });
});

describe("the auth pages with authentication on", () => {
  it("a guest sees the sign-in form", async () => {
    await visit("guest", "/login");

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText("home page")).not.toBeInTheDocument();
  });

  it("a guest sees the register form when registration is open", async () => {
    await visit("guest", "/register");

    expect(await screen.findByRole("button", { name: "Create account" })).toBeInTheDocument();
  });

  it("a guest is told registration is closed when it is", async () => {
    await visit("guest", "/register", { registration_open: false });

    expect(await screen.findByText("Registration closed")).toBeInTheDocument();
  });

  it("the return of a sign-in through a provider is reachable", async () => {
    await visit("guest", "/login/provider?error=account_pending");

    await waitFor(() => expect(screen.getByText("Waiting for approval")).toBeInTheDocument());
  });
});
