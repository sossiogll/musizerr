import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import { viewerStatus } from "@/test/viewer";
import { AppShell } from "./AppShell";
import type { AuthConfig, ViewerMode } from "@/types";

let status: AuthConfig;
let user: { id: string; username: string; role: string } | null = null;
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user, logout: vi.fn() }) }));
let playerTrack: { provider_id: string } | null = null;
vi.mock("@/contexts/PlayerContext", () => ({ usePlayer: () => ({ track: playerTrack }) }));
vi.mock("@/components/Player", () => ({ Player: () => null }));
// The animated background has its own tests (AppBackground.test.tsx).
vi.mock("./AppBackground", () => ({ AppBackground: () => null }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/lib/api-client", () => ({
  getLibraryJobs: async () => ({
    library_sync: { kind: "library_sync", status: "never_run" },
    download_sync: { kind: "download_sync", status: "never_run" },
  }),
  getPublicProviders: async () => ({ providers: [], metadata_priority: [], metadata: [], metadata_capabilities: {} }),
  getPendingRequestCount: async () => ({ pending: 4 }),
  getDownloadsSummary: async () => ({
    statuses: {}, phases: { queued: 2, active: 1, done: 10, failed: 1, review: 0 }, total: 14, downloaded_since: 3,
  }),
}));

function renderShell(mode: ViewerMode, path = "/") {
  status = viewerStatus(mode);
  user = mode === "user" || mode === "admin" ? { id: "u1", username: "alice", role: mode } : null;
  const root = createRootRoute({
    component: () => (
      <AppShell>
        <Outlet />
      </AppShell>
    ),
  });
  const pages = ["/", "/search", "/favorites", "/downloads", "/admin", "/config", "/about"].map((p) =>
    createRoute({ getParentRoute: () => root, path: p, component: () => <p>page {p}</p> }),
  );
  const router = createRouter({
    routeTree: root.addChildren(pages),
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <I18nProvider>
        <RouterProvider router={router} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

const navs = () => screen.getAllByRole("navigation", { name: "Main" });
const labels = (nav: HTMLElement) => within(nav).getAllByRole("link").map((a) => a.textContent?.replace(/\d+$/, "").trim());

beforeEach(() => {
  user = null;
  playerTrack = null;
});

describe("AppShell — one navigation, three forms", () => {
  it("renders the same items as sidebar (XL), top bar (L/M/S) and bottom bar (XS, primary only)", async () => {
    renderShell("admin");
    await screen.findByText("page /");
    const [sidebar, top, bottom] = navs();

    expect(labels(sidebar)).toEqual(["Home", "Search", "Favorites", "Playlists", "Downloads", "Administration", "Settings", "About"]);
    // The top bar's search field stands in for the Search item; labels are shorter there.
    expect(labels(top)).toEqual(["Home", "Favorites", "Playlists", "Downloads", "Admin", "Settings", "About"]);
    expect(screen.getAllByRole("search").length).toBeGreaterThan(0);
    expect(labels(bottom)).toEqual(["Home", "Search", "Favorites", "Playlists", "Downloads"]);
  });

  it("each form is shown by CSS breakpoints only", async () => {
    renderShell("admin");
    await screen.findByText("page /");
    const [sidebar, top, bottom] = navs();

    expect(sidebar.closest("aside")).toHaveClass("hidden", "xl:flex");
    expect(top.closest("header")).toHaveClass("hidden", "sm:flex", "xl:hidden");
    expect(bottom).toHaveClass("sm:hidden");
    // Top bar: S shows 44px icons (label for screen readers only), M labels only, L both.
    const link = within(top).getByRole("link", { name: /Favorites/ });
    expect(link).toHaveClass("max-md:w-11");
    expect(link.querySelector("svg")).toHaveClass("md:max-lg:hidden");
    expect(within(link).getByText("Favorites")).toHaveClass("max-md:sr-only");
  });

  it("a guest, who has no search field, keeps the Search item in the top bar", async () => {
    renderShell("guest");
    await screen.findByText("page /");
    expect(labels(navs()[1])).toEqual(["Home", "Search", "About"]);
    expect(screen.queryByRole("search")).not.toBeInTheDocument();
  });

  it.each([
    ["guest", ["Home", "Search", "About"]],
    ["user", ["Home", "Search", "Favorites", "Playlists", "Downloads", "Settings", "About"]],
    ["admin", ["Home", "Search", "Favorites", "Playlists", "Downloads", "Administration", "Settings", "About"]],
    ["open", ["Home", "Search", "Favorites", "Playlists", "Downloads", "Administration", "Settings", "About"]],
  ] as [ViewerMode, string[]][])("%s sees exactly the items their capabilities allow", async (mode, expected) => {
    renderShell(mode);
    await screen.findByText("page /");
    expect(labels(navs()[0])).toEqual(expected);
  });

  it("marks the current section, prefix included", async () => {
    renderShell("user", "/favorites");
    await screen.findByText("page /favorites");
    for (const nav of navs()) {
      expect(within(nav).getByRole("link", { current: "page" })).toHaveTextContent("Favorites");
    }
  });

  it("the Downloads badge counts queued and active tracks", async () => {
    renderShell("user");
    await screen.findByText("page /");
    expect((await screen.findAllByLabelText("3 in progress")).length).toBe(3);
  });

  describe("requests waiting for an admin", () => {
    it("an admin sees the count on Administration, in the sidebar and the top bar", async () => {
      renderShell("admin");
      await screen.findByText("page /");
      const [sidebar, top] = navs();

      expect(await within(sidebar).findByLabelText("4 requests waiting for approval")).toHaveTextContent("4");
      expect(within(sidebar).getByRole("link", { name: /Administration/ })).toContainElement(within(sidebar).getByLabelText("4 requests waiting for approval"));
      expect(within(top).getByRole("link", { name: /Admin/ })).toContainElement(within(top).getByLabelText("4 requests waiting for approval"));
    });

    it("on XS, where Administration is behind the avatar, the count is on the avatar and in the menu", async () => {
      renderShell("admin");
      await screen.findByText("page /");
      const mobile = screen.getAllByRole("banner").find((h) => h.classList.contains("sm:hidden"))!;
      const avatar = within(mobile).getByRole("button", { name: "Account and settings" });

      const badge = await within(avatar).findByLabelText("4 requests waiting for approval");
      expect(badge).toHaveTextContent("4");
      // Only on the XS header: from S up the top bar shows the item itself.
      expect(badge).toHaveClass("sm:hidden");

      fireEvent.keyDown(avatar, { key: "Enter" });
      const item = await screen.findByRole("menuitem", { name: /Administration/ });
      expect(within(item).getByLabelText("4 requests waiting for approval")).toBeInTheDocument();
    });

    it("a user has no requests to decide: no count, nothing asked of the server", async () => {
      renderShell("user");
      await screen.findByText("page /");
      expect(screen.queryByLabelText(/requests? waiting for approval/)).not.toBeInTheDocument();
    });
  });

  it("a guest gets 'Sign in' instead of the account menu", async () => {
    renderShell("guest");
    await screen.findByText("page /");
    expect(screen.getAllByRole("link", { name: "Sign in" }).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Account and settings" })).not.toBeInTheDocument();
  });

  it("with authentication off nothing offers to sign in, register or sign out: the account menu only leads to Settings and Administration", async () => {
    renderShell("open");
    await screen.findByText("page /");

    expect(screen.queryByRole("link", { name: /Sign in|Register|Create account/ })).not.toBeInTheDocument();
    expect(document.querySelector('a[href^="/login"], a[href^="/register"]')).toBeNull();
    const [avatar] = screen.getAllByRole("button", { name: "Account and settings" });
    fireEvent.keyDown(avatar, { key: "Enter" });
    await screen.findByRole("menuitem", { name: /Settings/ });
    expect(screen.queryByRole("menuitem", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.getAllByText("Sign-in off").length).toBeGreaterThan(0); // who the menu is about: nobody, said as the mode
  });

  it("a guest on XS, who has no account menu, still reaches About from the header", async () => {
    renderShell("guest");
    await screen.findByText("page /");
    const mobile = screen.getAllByRole("banner").find((h) => h.classList.contains("sm:hidden"))!;
    const about = within(mobile).getByRole("link", { name: "About" });
    expect(about).toHaveAttribute("href", "/about");
    // From S up the top bar shows the item itself.
    expect(about).toHaveClass("sm:hidden");
  });

  it("everyone else finds About in the account menu on XS", async () => {
    renderShell("user");
    await screen.findByText("page /");
    const mobile = screen.getAllByRole("banner").find((h) => h.classList.contains("sm:hidden"))!;
    fireEvent.keyDown(within(mobile).getByRole("button", { name: "Account and settings" }), { key: "Enter" });
    expect(await screen.findByRole("menuitem", { name: /About/ })).toBeInTheDocument();
  });

  it("the XS header names the page", async () => {
    renderShell("user", "/downloads");
    await screen.findByText("page /downloads");
    const header = screen.getAllByRole("banner").find((h) => h.classList.contains("sm:hidden"))!;
    expect(header).toHaveTextContent("Downloads");
  });

  it("content is fluid, up to 1600px", async () => {
    renderShell("user");
    await screen.findByText("page /");
    expect(screen.getByRole("main")).toHaveClass("max-w-[1600px]");
  });

  it("exposes the fixed chrome's size per tier: the top bar (none on XL) and the player when shown", async () => {
    renderShell("user");
    await screen.findByText("page /");
    const shell = screen.getByRole("main").parentElement!;
    expect(shell).toHaveClass("[--shell-top:3.5rem]", "sm:[--shell-top:4rem]", "xl:[--shell-top:0px]", "[--player-h:0px]");
  });

  it("the player height counts once a track is loaded", async () => {
    playerTrack = { provider_id: "t1" };
    renderShell("user");
    await screen.findByText("page /");
    const shell = screen.getByRole("main").parentElement!;
    expect(shell).toHaveClass("[--player-h:calc(4rem_+_1px)]", "sm:[--player-h:calc(5rem_+_1px)]");
    expect(shell).not.toHaveClass("[--player-h:0px]");
  });
});
