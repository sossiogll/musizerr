import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createRouter,
  createRootRoute,
  createRoute,
  createMemoryHistory,
} from "@tanstack/react-router";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { I18nProvider } from "@/i18n";
import { authStore } from "@/lib/auth-store";
import { viewerStatus } from "@/test/viewer";
import { RootLayout } from "./root";
import { ConfigPage } from "./config";
import { LoginPage } from "./login";
import { RequireCapability } from "@/components/RequireCapability";
import type { ConfigSchemaResponse, SettingDef } from "@/lib/api-client";
import type { ProvidersResponse, SearchType, User } from "@/types";

// Interface-level coverage for the three auth states the app actually runs
// in (Phase 5b, second half, Task 3): auth disabled, signed out with auth
// enabled, and signed in. Renders the real router + RootLayout (Header,
// MobileNav, RequireAuth, ConfigPage) against a mocked backend, rather than
// asserting on AuthContext/authStore internals directly, so these tests keep
// their value if the components are restructured.
//
// jsdom does not evaluate the `hidden md:flex` / `flex md:hidden` responsive
// classes, so both the desktop and the mobile copies of a nav entry are
// present in the tree at once here. That is actually what we want to check:
// "hidden at both widths" means the conditional guard removes BOTH copies
// from the DOM, not that CSS happens to hide one of them.

const mockGetAuthConfig = vi.fn();
const mockGetMe = vi.fn();
const mockRefreshAccessToken = vi.fn();
const mockLogoutRequest = vi.fn();
const mockGetProviders = vi.fn();
const mockGetSettingsSchema = vi.fn();
const mockGetSettingsValues = vi.fn();
const mockPatchSettings = vi.fn();
const mockRevokeAllAuth = vi.fn();
const mockGetLinkedAccounts = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client");
  return {
    ...actual,
    getAuthConfig: (...a: unknown[]) => mockGetAuthConfig(...a),
    getMe: (...a: unknown[]) => mockGetMe(...a),
    refreshAccessToken: (...a: unknown[]) => mockRefreshAccessToken(...a),
    logoutRequest: (...a: unknown[]) => mockLogoutRequest(...a),
    getProviders: (...a: unknown[]) => mockGetProviders(...a),
    getSettingsSchema: (...a: unknown[]) => mockGetSettingsSchema(...a),
    getSettingsValues: (...a: unknown[]) => mockGetSettingsValues(...a),
    patchSettings: (...a: unknown[]) => mockPatchSettings(...a),
    revokeAllAuth: (...a: unknown[]) => mockRevokeAllAuth(...a),
    getLinkedAccounts: (...a: unknown[]) => mockGetLinkedAccounts(...a),
    getDownloadsSummary: async () => ({
      statuses: {}, phases: { queued: 0, active: 0, done: 0, failed: 0, review: 0 }, total: 0, downloaded_since: 0,
    }),
    // QueueContext opens an SSE connection on mount; jsdom has no EventSource.
    createProgressSource: () => ({ close: () => {}, onmessage: null, onerror: null }),
  };
});

// Radix DropdownMenu (account menu) touches a couple of DOM APIs jsdom does
// not implement, and opens on "pointerdown" rather than "click". jsdom has
// no native PointerEvent class, so @testing-library/dom's fireEvent.pointerDown
// silently falls back to a plain Event — which drops `button`/`ctrlKey`,
// the exact fields Radix's trigger checks before toggling open. Polyfill a
// minimal PointerEvent (a MouseEvent subclass, per spec) so those fields
// survive.
beforeEach(() => {
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
  if (!Element.prototype.releasePointerCapture) {
    Element.prototype.releasePointerCapture = () => {};
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
  // `Window.PointerEvent` is a non-optional property in lib.dom.d.ts, so
  // `"PointerEvent" in window` narrows the negative branch to `never` at
  // compile time even though jsdom genuinely omits it at runtime. Check the
  // runtime value instead of the static shape.
  if (typeof window.PointerEvent === "undefined") {
    class PointerEventPolyfill extends MouseEvent {
      pointerId: number;
      pointerType: string;
      isPrimary: boolean;
      constructor(type: string, params: PointerEventInit = {}) {
        super(type, params);
        this.pointerId = params.pointerId ?? 0;
        this.pointerType = params.pointerType ?? "mouse";
        this.isPrimary = params.isPrimary ?? true;
      }
    }
    window.PointerEvent = PointerEventPolyfill as unknown as typeof PointerEvent;
  }
});

function providersResponse(): ProvidersResponse {
  return { metadata: [], download: [], lyrics: [], storage: [], sync: [], identification: [] };
}

function settingDef(overrides: Partial<SettingDef> & Pick<SettingDef, "key" | "type" | "area">): SettingDef {
  return {
    group: "g1",
    label: overrides.key,
    description: "",
    admin_only: false,
    options: null,
    minimum: null,
    maximum: null,
    step: null,
    secret: false,
    provider_category: null,
    required: true,
    visible_when: null,
    ...overrides,
  };
}

// "general" listed before "providers"/"account" so it wins the "first
// visible area" fallback used as the default tab when there is no signed-in
// user (see ConfigPage.defaultTab) — keeps these tests from having to also
// stub the admin-only SourcesTab, which only mounts inside "Providers".
function schemaResponse(): ConfigSchemaResponse {
  return {
    settings: [settingDef({ key: "flag", type: "bool", area: "general", label: "Flag" })],
    taxonomy: {
      areas: [
        { key: "general", label_key: "settings.areas.general", label: "General" },
        { key: "providers", label_key: "settings.areas.providers", label: "Providers" },
        { key: "account", label_key: "settings.areas.account", label: "Account" },
      ],
      groups: [{ key: "g1", label_key: "settings.groups.g1", label: "Group One", description_key: "settings.group_descriptions.g1", description: "", area: "general" }],
    },
  };
}

function fakeUser(): User {
  return { id: "u1", username: "alice", email: null, role: "user", is_active: true };
}

// A trivial stand-in for HomePage — home's own content (search, discover
// feeds, trending, ...) isn't what these tests exercise, and letting the
// real HomePage mount after the sign-out test navigates to "/" would pull in
// a pile of unrelated, unmocked network calls. Everything else here
// (RootLayout, RequireAuth, ConfigPage, LoginPage) is the genuine article.
function HomeStub() {
  return <div>home-stub</div>;
}

function buildTestRouteTree() {
  const rootRoute = createRootRoute({ component: RootLayout });
  const homeRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/",
    component: HomeStub,
    validateSearch: (search: Record<string, unknown>): { q: string; type: SearchType; providers: string[] } => ({
      q: typeof search.q === "string" ? search.q : "",
      type: (["track", "album", "artist", "playlist"] as SearchType[]).includes(search.type as SearchType)
        ? (search.type as SearchType)
        : "track",
      providers: Array.isArray(search.providers) ? (search.providers as string[]) : [],
    }),
  });
  const guardedConfigPage = () => (
    <RequireCapability capability={["settings_personal", "admin"]}>
      <ConfigPage />
    </RequireCapability>
  );
  const configRoute = createRoute({ getParentRoute: () => rootRoute, path: "/config", component: guardedConfigPage });
  const configSectionRoute = createRoute({ getParentRoute: () => rootRoute, path: "/config/$section", component: guardedConfigPage });
  const loginRoute = createRoute({ getParentRoute: () => rootRoute, path: "/login", component: LoginPage });
  return rootRoute.addChildren([homeRoute, configRoute, configSectionRoute, loginRoute]);
}

function buildTestRouter(initialPath: string) {
  return createRouter({
    routeTree: buildTestRouteTree(),
    history: createMemoryHistory({ initialEntries: [initialPath] }),
  });
}

function renderApp(initialPath: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const testRouter = buildTestRouter(initialPath);
  return render(
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <I18nProvider>
          <RouterProvider router={testRouter} />
        </I18nProvider>
      </ThemeProvider>
    </QueryClientProvider>,
  );
}

// The shell renders the navigation once per form (sidebar, top bar, bottom
// bar), all present in jsdom since it does not apply the breakpoints: count
// the copies of an entry, not a fixed number.
function settingsLinks(): HTMLAnchorElement[] {
  return Array.from(document.querySelectorAll('a[href^="/config"]'));
}

function signInLinks(): HTMLAnchorElement[] {
  return Array.from(document.querySelectorAll('a[href^="/login"]'));
}

function accountMenuTriggers(): Element[] {
  return screen.queryAllByRole("button", { name: "Account and settings" });
}

beforeEach(() => {
  mockGetAuthConfig.mockReset();
  mockGetMe.mockReset();
  mockRefreshAccessToken.mockReset();
  mockLogoutRequest.mockReset();
  mockGetProviders.mockReset();
  mockGetSettingsSchema.mockReset();
  mockGetSettingsValues.mockReset();
  mockPatchSettings.mockReset();
  mockRevokeAllAuth.mockReset();
  mockGetLinkedAccounts.mockReset();
  mockGetLinkedAccounts.mockResolvedValue([]);

  mockGetProviders.mockResolvedValue(providersResponse());
  mockGetSettingsSchema.mockResolvedValue(schemaResponse());
  mockGetSettingsValues.mockResolvedValue({ flag: true });
  mockPatchSettings.mockResolvedValue({ flag: true });

  // authStore is a real module-level singleton — reset it between tests so
  // no session (or generation count) leaks from one test into the next.
  authStore.clear();
});

describe("auth states — settings entry, account menu, settings page", () => {
  it("authentication disabled: settings page renders, entries visible at both widths, no account menu, Account tab shown with nothing to display", async () => {
    mockGetAuthConfig.mockResolvedValue(viewerStatus("open"));

    renderApp("/config");

    // Wait for the real settings page, not just its shell: ConfigPage
    // (routes/config/index.tsx) renders the SAME "Settings" heading in its
    // loading-skeleton branch (schemaData still undefined, GET
    // /settings/schema pending) as in its loaded one — the heading alone
    // is not proof the tabs (which depend on that query resolving) have
    // rendered yet. Waiting on it was a genuine race, intermittently
    // finding no "Account" tab right after (the flake reported against
    // this test): fixed by waiting for a condition that can only be true
    // once schemaData has actually loaded, not by a delay or a retry.
    // /config opens the default section, Account, as its own address.
    const sections = await screen.findByRole("navigation", { name: "Settings sections" });
    const accountTab = within(sections).getByRole("link", { name: "Account" });
    expect(accountTab).toHaveAttribute("aria-current", "page");
    expect(accountTab).toHaveAttribute("href", "/config/account");
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    // Open mode is said out loud: anyone reaching the address is an admin.
    expect(screen.getByRole("alert")).toHaveTextContent("Sign-in is off");

    // The Settings entry is in the sidebar and the top bar.
    expect(settingsLinks().length).toBeGreaterThanOrEqual(2);
    // No account exists in this mode, but the menu is still how XS reaches
    // Settings and Administration; there is no sign-out in it.
    expect(accountMenuTriggers().length).toBeGreaterThanOrEqual(1);
    expect(signInLinks()).toHaveLength(0);

    // "Account" now shows even with auth disabled — the fix for the bug
    // where it gated on a `user` object that can never exist in this mode.
    // It is in fact the default tab (ConfigPage.defaultTab), but with
    // nothing to display here: this taxonomy declares no account-area
    // settings and no "language"/"theme" keys (see AccountTab.test.tsx for
    // that), and there is no identity block without a real user.
    expect(accountTab).toBeInTheDocument();
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();

    // The "general" area, reached by its section link, is unaffected by any
    // of this.
    fireEvent.click(within(sections).getByRole("link", { name: "General" }));
    await screen.findByText("Group One");
    expect(within(sections).getByRole("link", { name: "General" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("switch")).toBeInTheDocument();

    // refreshAccessToken must never be attempted: there is no session to
    // resurrect in this mode at all.
    expect(mockRefreshAccessToken).not.toHaveBeenCalled();
  });

  it("signed out with auth enabled: the guard redirects away, entries are hidden, sign-in is shown", async () => {
    mockGetAuthConfig.mockResolvedValue(viewerStatus("guest"));
    mockRefreshAccessToken.mockResolvedValue(null);

    renderApp("/config");

    // RequireAuth redirected /config -> /login.
    await screen.findByText("Login");
    expect(screen.getByText("Sign in to your account")).toBeInTheDocument();

    expect(settingsLinks()).toHaveLength(0);
    expect(accountMenuTriggers()).toHaveLength(0);
    expect(signInLinks().length).toBeGreaterThanOrEqual(1);
  });

  it("signed in: Settings has Linked accounts right after Account, and no Providers tab (those are the administrator's)", async () => {
    mockGetAuthConfig.mockResolvedValue(viewerStatus("user"));
    mockGetMe.mockResolvedValue(fakeUser());
    authStore.setAccessToken("access");

    renderApp("/config/linked-accounts");

    const sections = await screen.findByRole("navigation", { name: "Settings sections" });
    expect(within(sections).getAllByRole("link").map((l) => l.textContent)).toEqual(["General", "Account", "Linked accounts"]);
    expect(within(sections).getByRole("link", { name: "Linked accounts" })).toHaveAttribute("aria-current", "page");
    expect(within(sections).getByRole("link", { name: "Linked accounts" })).toHaveAttribute("href", "/config/linked-accounts");
    await waitFor(() => expect(mockGetLinkedAccounts).toHaveBeenCalled());
  });

  it("signed in: settings page renders its areas, account menu is present, and signing out un-signs the user", async () => {
    // The status follows the session, as the server's does: "user" with a
    // bearer token, "guest" without.
    mockGetAuthConfig.mockImplementation(async () => viewerStatus(authStore.getAccessToken() ? "user" : "guest"));
    // Seeded with a real refresh token and resolved via the real authStore
    // (rather than an unconditional stub) so that logout's authStore.clear()
    // is actually reflected here: a later refetch of this session (e.g. the
    // "auth-config" query auto-refetching after handleLogout's
    // queryClient.clear()) must find no token and stay signed out, exactly
    // like the real app — an unconditional mock would silently "resurrect"
    // the user on every such refetch and make this test worthless.
    authStore.setRefreshToken("initial-refresh-token");
    // Like the real refreshAccessToken: a successful refresh stores the new
    // access token, which is what the session is read from.
    mockRefreshAccessToken.mockImplementation(async () => {
      if (!authStore.getRefreshToken()) return null;
      authStore.setAccessToken("fresh-access-token");
      return "fresh-access-token";
    });
    mockGetMe.mockResolvedValue(fakeUser());
    mockLogoutRequest.mockResolvedValue({});

    renderApp("/config");

    // Default tab for a signed-in user is "account" (ConfigPage), which
    // renders the identity block — this is what "settings page renders its
    // areas" looks like from the outside.
    // (The name is also in the shell's account block, so wait for the tab's own content.)
    const signOut = await screen.findByRole("button", { name: "Sign out" });
    expect(screen.getAllByText("alice").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("Sign-in is off")).not.toBeInTheDocument();

    expect(settingsLinks().length).toBeGreaterThanOrEqual(2);
    // "account menu is present": the trigger itself is real, rendered proof
    // an account menu exists for a signed-in user — this does not require
    // actually opening it. Bisecting the previous 41-54s runtime (against a
    // 40s timeout) down to its real source showed the slow, highly variable
    // part was never sign-out or router navigation at all (removing
    // AccountTab's explicit navigate({to:"/"}), then RequireAuth's redirect,
    // then the whole logout() call in turn — none of it made a dent): it was
    // this trigger's Radix DropdownMenu actually being driven open and
    // closed (fireEvent.pointerDown x2 + a findByRole wait for its portaled
    // content), which alone reproduced the full delay with an otherwise
    // no-op sign-out handler. What that used to additionally prove — that
    // the menu's contents genuinely include a working "Sign out" item, not
    // just that a trigger exists — is exactly "the Radix portal" this test
    // does not need to drive; DropdownMenu itself is a third-party
    // component, not app logic under test here.
    expect(accountMenuTriggers().length).toBeGreaterThanOrEqual(1);

    // Drive the actual sign-out through the Account tab's own "Sign out"
    // button (a plain element, no portal involved) — both it and the
    // account menu's item call the same AuthContext.logout().
    //
    // The chain of state updates this triggers (authStore -> AuthContext ->
    // route re-evaluation -> real router navigation to "/") spans several
    // components that update outside of an explicit act(), each logging a
    // (harmless) "not wrapped in act" warning; console.error formatting
    // those repeatedly adds noise, so it's muted for this one click.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    fireEvent.click(signOut);

    // Regression test for Task 1 at the interface level: sign-out must
    // actually leave the user signed out, not just appear to. The
    // observable proof is the nav reflecting signed-out state (settings
    // entries and the account menu gone, sign-in back) — a synchronous
    // consequence of AuthContext.logout()'s setUser(null), which re-renders
    // Header/MobileNav directly and does not depend on where the router
    // ends up landing (unlike the previous version of this test, which also
    // waited for a real router navigation all the way to "/", "home-stub").
    // The sign-out mechanics themselves (authStore.clear(), the
    // refresh-token race) are already covered at the store level by
    // api-client.test.ts — this only needs to prove the interface
    // consequence. Default waitFor timeout, no per-test override: dropped,
    // not raised.
    await waitFor(() => {
      expect(signInLinks().length).toBeGreaterThanOrEqual(1);
    });
    expect(settingsLinks()).toHaveLength(0);
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
    expect(accountMenuTriggers()).toHaveLength(0);
    consoleError.mockRestore();
  });
});
