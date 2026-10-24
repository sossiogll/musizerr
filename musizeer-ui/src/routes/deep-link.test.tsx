import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider, dehydrate } from "@tanstack/react-query";
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  useSearch,
} from "@tanstack/react-router";
import { RequireCapability } from "@/components/RequireCapability";
import { AppGate } from "@/components/connectivity/AppGate";
import { AuthProvider } from "@/contexts/AuthContext";
import { I18nProvider } from "@/i18n";
import { authStore } from "@/lib/auth-store";
import { queryKeys, type ViewerScope } from "@/lib/query-keys";
import {
  CACHE_VERSION,
  STORAGE_KEY,
  isPersistedQuery,
  resetPersistenceForTests,
  restoreQueryCache,
} from "@/lib/query-persistence";
import { connectivityMock } from "@/test/connectivity-mock";
import { viewerStatus } from "@/test/viewer";
import type { AuthConfig, User, ViewerMode } from "@/types";

/**
 * Opening a guarded page directly, the way a bookmark or a reload does: the
 * real AuthProvider, AppGate, guard and offline copy, with the API faked.
 * The status answers for the bearer token, like the server's, and a little
 * later than /me, which is how the page used to be judged on a guest status
 * that was not the session's.
 */
const ADMIN: User = { id: "u-admin", username: "root", email: null, role: "admin", is_active: true };
const STATUS_DELAY_MS = 60;

const mockRefresh = vi.fn();

vi.mock("@/lib/api-client", () => ({
  getAuthConfig: () => {
    const status = viewerStatus(authStore.getAccessToken() ? "admin" : "guest");
    return new Promise<AuthConfig>((resolve) => setTimeout(() => resolve(status), STATUS_DELAY_MS));
  },
  getMe: async () => {
    if (!authStore.getAccessToken()) throw new Error("401");
    return ADMIN;
  },
  refreshAccessToken: () => mockRefresh(),
  login: vi.fn(),
  logoutRequest: vi.fn(),
}));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

function LoginProbe() {
  const { redirect } = useSearch({ strict: false }) as { redirect?: string };
  return <p>login page, back to {redirect}</p>;
}

/** What the browser had saved by the last visit: a status per scope, and the viewer if there was one. */
function saveCopy(scope: ViewerScope, mode: ViewerMode) {
  const seed = new QueryClient();
  seed.setQueryData(queryKeys.viewerStatus(scope), viewerStatus(mode));
  if (mode !== "guest") seed.setQueryData(queryKeys.me, ADMIN);
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      v: CACHE_VERSION,
      savedAt: Date.now(),
      owner: mode === "guest" ? "guest" : ADMIN.id,
      state: dehydrate(seed, { shouldDehydrateQuery: isPersistedQuery }),
    }),
  );
}

/** The access token the refresh token buys, after a moment like a real round trip. */
function sessionRestores() {
  authStore.setRefreshToken("rt");
  mockRefresh.mockImplementation(async () => {
    await new Promise((resolve) => setTimeout(resolve, 10));
    authStore.setAccessToken("at");
    return "at";
  });
}

/** The session is over: the server refuses the refresh token. */
function sessionExpired() {
  authStore.setRefreshToken("rt");
  mockRefresh.mockImplementation(async () => {
    authStore.clear();
    return null;
  });
}

const visited: string[] = [];

function openDeepLink(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  restoreQueryCache(queryClient);
  const root = createRootRoute({
    component: () => (
      <AuthProvider>
        <AppGate>
          <Outlet />
        </AppGate>
      </AuthProvider>
    ),
  });
  const requests = createRoute({
    getParentRoute: () => root,
    path: "/admin/requests",
    component: () => (
      <RequireCapability capability="admin">
        <p>requests page</p>
      </RequireCapability>
    ),
  });
  const login = createRoute({ getParentRoute: () => root, path: "/login", component: LoginProbe });
  const history = createMemoryHistory({ initialEntries: [path] });
  history.subscribe(() => visited.push(history.location.pathname));
  render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <RouterProvider router={createRouter({ routeTree: root.addChildren([requests, login]), history })} />
      </I18nProvider>
    </QueryClientProvider>,
  );
  return history;
}

const settle = (ms = STATUS_DELAY_MS * 3) => act(() => new Promise((resolve) => setTimeout(resolve, ms)));

beforeEach(() => {
  localStorage.clear();
  authStore.clear();
  resetPersistenceForTests();
  connectivityMock.reset();
  mockRefresh.mockReset();
  visited.length = 0;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("a deep link to a guarded page", () => {
  it("right after the session is restored, waits for the session's status instead of judging the saved guest one", async () => {
    saveCopy("anonymous", "guest");
    sessionRestores();

    const history = openDeepLink("/admin/requests");

    expect(await screen.findByText("requests page")).toBeInTheDocument();
    expect(history.location.pathname).toBe("/admin/requests");
    expect(visited).not.toContain("/login");
    // And it is still there once every request has settled.
    await settle();
    expect(screen.getByText("requests page")).toBeInTheDocument();
  });

  it("with nothing saved at all, shows the loading state, never the login page", async () => {
    sessionRestores();

    openDeepLink("/admin/requests");

    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(await screen.findByText("requests page")).toBeInTheDocument();
    expect(visited).not.toContain("/login");
  });

  it("an expired session goes to sign in, and comes back to the page afterwards", async () => {
    saveCopy("session", "admin");
    sessionExpired();

    openDeepLink("/admin/requests");

    expect(await screen.findByText("login page, back to /admin/requests")).toBeInTheDocument();
    // The saved administrator's status never opened the page on the way.
    expect(screen.queryByText("requests page")).not.toBeInTheDocument();
  });

  it("a guest is sent to sign in", async () => {
    saveCopy("anonymous", "guest");

    openDeepLink("/admin/requests");

    expect(await screen.findByText("login page, back to /admin/requests")).toBeInTheDocument();
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it("with the API unreachable, a signed-in viewer keeps the page from the saved copy and stays signed in", async () => {
    saveCopy("session", "admin");
    authStore.setRefreshToken("rt");
    connectivityMock.set({ online: false });

    const history = openDeepLink("/admin/requests");

    expect(await screen.findByText("requests page")).toBeInTheDocument();
    expect(history.location.pathname).toBe("/admin/requests");
    expect(mockRefresh).not.toHaveBeenCalled();
    expect(authStore.getRefreshToken()).toBe("rt");
    expect(visited).not.toContain("/login");
  });

  it("when the API comes back the session is restored and the page stays", async () => {
    saveCopy("session", "admin");
    sessionRestores();
    connectivityMock.set({ online: false });

    openDeepLink("/admin/requests");
    expect(await screen.findByText("requests page")).toBeInTheDocument();

    act(() => connectivityMock.set({ online: true }));
    await settle();

    expect(mockRefresh).toHaveBeenCalledTimes(1);
    expect(screen.getByText("requests page")).toBeInTheDocument();
    expect(visited).not.toContain("/login");
  });
});
