import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import { viewerStatus } from "@/test/viewer";
import { SearchPage } from "./search";
import type { AuthConfig, Track } from "@/types";

let status: AuthConfig = viewerStatus("guest");
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: null }) }));

const mockSearch = vi.fn();
const preview: Partial<Track> = {
  kind: "track", title: "Preview Song", artist: "A", album: "B", albums: [], artists: [], providers: [],
  duration_ms: 1000, cover_url: null, preview_url: "https://cdn.example/p.mp3", stream_url: null,
  provider: "p", provider_id: "1", external_ids: [], year: null,
};
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  search: (...a: unknown[]) => mockSearch(...a),
  getTrendingTracks: async () => ({ items: [preview, { ...preview, provider_id: "2", title: "No preview", preview_url: null }], provider_errors: [] }),
  getPublicProviders: async () => ({ providers: [], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
}));

function renderSearch() {
  const root = createRootRoute();
  const route = createRoute({
    getParentRoute: () => root,
    path: "/search",
    component: SearchPage,
    validateSearch: () => ({ q: "love", type: "track" as const, providers: [] as string[] }),
  });
  const login = createRoute({ getParentRoute: () => root, path: "/login", component: () => <p>login</p> });
  const router = createRouter({
    routeTree: root.addChildren([route, login]),
    history: createMemoryHistory({ initialEntries: ["/search"] }),
  });
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider>
        <RouterProvider router={router} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

describe("Search — guest showcase", () => {
  it("a guest is invited to sign in and never searches (no 401)", async () => {
    status = viewerStatus("guest");
    renderSearch();

    expect(await screen.findByRole("heading", { name: "Sign in to search" })).toBeInTheDocument();
    // Signing in comes back to this search, with what was typed.
    const href = screen.getByRole("link", { name: "Sign in" }).getAttribute("href") ?? "";
    expect(href.startsWith("/login?")).toBe(true);
    expect(new URLSearchParams(href.split("?")[1]).get("redirect")).toMatch(/^\/search\?q=love/);
    expect(mockSearch).not.toHaveBeenCalled();
    // Only tracks that have a preview are offered to a guest.
    expect(await screen.findByText("Preview Song")).toBeInTheDocument();
    expect(screen.queryByText("No preview")).not.toBeInTheDocument();
    // No download buttons for a guest.
    expect(screen.queryByRole("button", { name: /download/i })).not.toBeInTheDocument();
  });

  it("a signed-in user gets the search", async () => {
    status = viewerStatus("user");
    mockSearch.mockResolvedValue({ type: "track", query: "love", items: [], total: 0, page: 1, limit: 0, pages: 1, provider_errors: [] });
    renderSearch();

    expect(await screen.findByPlaceholderText("Search tracks, albums, artists…")).toBeInTheDocument();
    expect(screen.queryByText("Sign in to search")).not.toBeInTheDocument();
  });
});
