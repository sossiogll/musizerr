import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider, useQuery, type QueryClient } from "@tanstack/react-query";
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import { AppShell } from "@/components/shell/AppShell";
import { authStore } from "@/lib/auth-store";
import { createQueryClient } from "@/lib/query-client";
import { queryKeys } from "@/lib/query-keys";
import {
  STORAGE_KEY,
  persistQueryCache,
  resetPersistenceForTests,
  restoreQueryCache,
} from "@/lib/query-persistence";
import { viewerStatus } from "@/test/viewer";
import { AuthProvider, useAuth } from "./AuthContext";

/**
 * The real AuthProvider, query client and offline copy, with the API
 * faked: who the viewer is follows the bearer token, like the server. The
 * status answers a little later than /me, as it did when the bug was found
 * (sign-in left the navigation in guest mode until a reload).
 */
const USERS: Record<string, { id: string; username: string; role: string }> = {
  "tok-alice": { id: "u-alice", username: "alice", role: "user" },
  "tok-bob": { id: "u-bob", username: "bob", role: "user" },
};
const current = () => USERS[authStore.getAccessToken() ?? ""] ?? null;
const later = <T,>(value: () => T, ms = 30) => new Promise<T>((resolve) => setTimeout(() => resolve(value()), ms));

vi.mock("@/lib/api-client", () => ({
  getAuthConfig: () => {
    const status = viewerStatus(current() ? "user" : "guest");
    return later(() => status);
  },
  getMe: async () => {
    const user = current();
    if (!user) throw new Error("401");
    return user;
  },
  login: async (username: string) => ({ access_token: `tok-${username}`, refresh_token: `rt-${username}` }),
  logoutRequest: async () => {},
  refreshAccessToken: async () => null,
  getDownloadsSummary: async () => ({ statuses: {}, phases: {}, total: 0, downloaded_since: 0 }),
  getFavorites: async () => ({ items: [`${current()?.username ?? "nobody"}'s track`] }),
  getLibraryJobs: async () => ({
    library_sync: { kind: "library_sync", status: "never_run" },
    download_sync: { kind: "download_sync", status: "never_run" },
  }),
  getPublicProviders: async () => ({ providers: [], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
}));
vi.mock("@/contexts/PlayerContext", () => ({ usePlayer: () => ({ track: null }) }));
vi.mock("@/components/Player", () => ({ Player: () => null }));
vi.mock("@/components/shell/AppBackground", () => ({ AppBackground: () => null }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

function Library() {
  const { user } = useAuth();
  const saved = useQuery({
    queryKey: queryKeys.favorites(),
    queryFn: async () => (await import("@/lib/api-client")).getFavorites(),
    enabled: !!user,
  });
  return <p data-testid="library">{(saved.data as { items: string[] } | undefined)?.items.join(",") ?? "-"}</p>;
}

function Controls() {
  const { login, logout, user } = useAuth();
  return (
    <div>
      <p data-testid="who">{user?.username ?? "guest"}</p>
      <button onClick={() => void login("alice", "pw")}>as alice</button>
      <button onClick={() => void login("bob", "pw")}>as bob</button>
      <button onClick={() => void logout()}>out</button>
      <Library />
    </div>
  );
}

let queryClient: QueryClient;
let stopPersisting: () => void;

function renderApp() {
  queryClient = createQueryClient();
  restoreQueryCache(queryClient);
  stopPersisting = persistQueryCache(queryClient);
  const root = createRootRoute({
    component: () => (
      <AuthProvider>
        <AppShell>
          <Outlet />
        </AppShell>
      </AuthProvider>
    ),
  });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: Controls });
  const router = createRouter({ routeTree: root.addChildren([home]), history: createMemoryHistory() });
  render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <RouterProvider router={router} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

/** The guest navigation is also what an unknown viewer sees: wait for the status itself. */
async function signedOut() {
  await waitFor(() => expect(queryClient.getQueryData(queryKeys.viewerStatus("anonymous"))).toMatchObject({ mode: "guest" }));
  expect(navLabels()).toEqual(GUEST_NAV);
}

const sidebar = () => screen.getAllByRole("navigation", { name: "Main" })[0];
const navLabels = () =>
  within(sidebar())
    .getAllByRole("link")
    .map((a) => a.textContent?.replace(/\d+$/, "").trim());
const click = (name: string) => act(() => screen.getByRole("button", { name }).click());
const USER_NAV = ["Home", "Search", "Favorites", "Playlists", "Downloads", "Settings", "About"];
const GUEST_NAV = ["Home", "Search", "About"];

beforeEach(() => {
  localStorage.clear();
  authStore.clear();
  resetPersistenceForTests();
});

afterEach(() => {
  stopPersisting();
  queryClient.clear();
});

describe("AuthProvider — the viewer changes without a reload", () => {
  it("guest → sign in: the navigation becomes the user's", async () => {
    renderApp();
    await signedOut();

    await click("as alice");

    await waitFor(() => expect(screen.getByTestId("who")).toHaveTextContent("alice"));
    await waitFor(() => expect(navLabels()).toEqual(USER_NAV));
    // And it stays that way once every request has settled.
    await act(() => new Promise((r) => setTimeout(r, 150)));
    expect(navLabels()).toEqual(USER_NAV);
    expect(queryClient.getQueryData(queryKeys.viewerStatus("session"))).toMatchObject({ mode: "user" });
  });

  it("user → sign out: the navigation becomes the guest's", async () => {
    renderApp();
    await signedOut();
    await click("as alice");
    await waitFor(() => expect(navLabels()).toEqual(USER_NAV));

    await click("out");

    await waitFor(() => expect(screen.getByTestId("who")).toHaveTextContent("guest"));
    await waitFor(() => expect(navLabels()).toEqual(GUEST_NAV));
    await act(() => new Promise((r) => setTimeout(r, 150)));
    expect(navLabels()).toEqual(GUEST_NAV);
  });

  it("user A → sign out → user B never sees A's library, in memory or on disk", async () => {
    renderApp();
    await signedOut();
    await click("as alice");
    await waitFor(() => expect(screen.getByTestId("library")).toHaveTextContent("alice's track"));
    act(() => {
      queryClient.setQueryData(queryKeys.downloads(), { items: ["alice's download"] });
    });
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toContain("alice's download"));

    await click("out");
    await waitFor(() => expect(navLabels()).toEqual(GUEST_NAV));
    await click("as bob");

    const seen: string[] = [];
    await waitFor(() => {
      seen.push(screen.getByTestId("library").textContent ?? "");
      expect(screen.getByTestId("library")).toHaveTextContent("bob's track");
    });
    expect(seen.join(" ")).not.toContain("alice");
    expect(queryClient.getQueryData(queryKeys.downloads())).toBeUndefined();
    await act(() => new Promise((r) => setTimeout(r, 1_200)));
    expect(localStorage.getItem(STORAGE_KEY) ?? "").not.toContain("alice");
    expect(navLabels()).toEqual(USER_NAV);
  });
});
