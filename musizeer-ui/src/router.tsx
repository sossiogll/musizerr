import { createRouter, createRoute, createRootRoute, lazyRouteComponent, redirect } from "@tanstack/react-router";
import { RootLayout } from "./routes/root";
import { NotFoundPage } from "./routes/not-found";
import { RouteError } from "./components/RouteError";
import type { SearchType } from "./types";
import { parseDownloadsSearch } from "./routes/downloads-search";
import { OpenModeRedirect } from "./components/OpenModeRedirect";
import { RequireCapability } from "./components/RequireCapability";
import { safeRedirect } from "./lib/safe-redirect";

/**
 * Every page is its own chunk, loaded when its route is first matched: the
 * shell (root layout, error and not-found pages) is all the first load
 * needs. Once the app is up, preloadPages() fetches the rest in the
 * background, so moving around keeps working if the connection drops later.
 */
const pages = {
  home: () => import("./routes/home"),
  search: () => import("./routes/search"),
  track: () => import("./routes/track"),
  album: () => import("./routes/album"),
  artist: () => import("./routes/artist"),
  playlist: () => import("./routes/playlist"),
  downloads: () => import("./routes/downloads"),
  config: () => import("./routes/config"),
  login: () => import("./routes/login"),
  loginProvider: () => import("./routes/login-provider"),
  register: () => import("./routes/register"),
  favorites: () => import("./routes/favorites"),
  playlists: () => import("./routes/playlists"),
  userPlaylist: () => import("./routes/user-playlist"),
  adminLayout: () => import("./routes/admin/layout"),
  adminUsers: () => import("./routes/admin/users"),
  adminRequests: () => import("./routes/admin/requests"),
  adminProviders: () => import("./routes/admin/providers"),
  adminDuplicates: () => import("./routes/admin/duplicates"),
  routines: () => import("./routes/config/RoutinesPanel"),
  sources: () => import("./routes/admin/sources"),
  settings: () => import("./routes/config/settings/SettingsArea"),
  about: () => import("./routes/about"),
};

export function preloadPages(): Promise<PromiseSettledResult<unknown>[]> {
  return Promise.allSettled(Object.values(pages).map((load) => load()));
}

const HomePage = lazyRouteComponent(pages.home, "HomePage");
const SearchPage = lazyRouteComponent(pages.search, "SearchPage");
const TrackPage = lazyRouteComponent(pages.track, "TrackPage");
const AlbumPage = lazyRouteComponent(pages.album, "AlbumPage");
const ArtistPage = lazyRouteComponent(pages.artist, "ArtistPage");
const PlaylistPage = lazyRouteComponent(pages.playlist, "PlaylistPage");
const DownloadsPage = lazyRouteComponent(pages.downloads, "DownloadsPage");
const ConfigPage = lazyRouteComponent(pages.config, "ConfigPage");
const LoginPage = lazyRouteComponent(pages.login, "LoginPage");
const ProviderLoginReturnPage = lazyRouteComponent(pages.loginProvider, "ProviderLoginReturnPage");
const RegisterPage = lazyRouteComponent(pages.register, "RegisterPage");
const FavoritesPage = lazyRouteComponent(pages.favorites, "FavoritesPage");
const PlaylistsPage = lazyRouteComponent(pages.playlists, "PlaylistsPage");
const UserPlaylistPage = lazyRouteComponent(pages.userPlaylist, "UserPlaylistPage");
const AdminLayout = lazyRouteComponent(pages.adminLayout, "AdminLayout");
const AdminUsersPage = lazyRouteComponent(pages.adminUsers, "AdminUsersPage");
const AdminRequestsPage = lazyRouteComponent(pages.adminRequests, "AdminRequestsPage");
const AdminProvidersPage = lazyRouteComponent(pages.adminProviders, "AdminProvidersPage");
const AdminDuplicatesPage = lazyRouteComponent(pages.adminDuplicates, "AdminDuplicatesPage");
const RoutinesPanel = lazyRouteComponent(pages.routines, "RoutinesPanel");
const AdminSourcesPage = lazyRouteComponent(pages.sources, "AdminSourcesPage");
const SettingsArea = lazyRouteComponent(pages.settings, "SettingsArea");
const AboutPage = lazyRouteComponent(pages.about, "AboutPage");

const rootRoute = createRootRoute({ component: RootLayout });

const homeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: HomePage,
});

const searchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/search",
  component: SearchPage,
  validateSearch: (search: Record<string, unknown>): { q: string; type: SearchType; providers: string[] } => ({
    q: typeof search.q === "string" ? search.q : "",
    type: (["track", "album", "artist", "playlist"] as SearchType[]).includes(search.type as SearchType)
      ? (search.type as SearchType)
      : "track",
    providers: Array.isArray(search.providers) ? (search.providers as string[]) : [],
  }),
});

const trackRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/track/$provider/$id",
  component: TrackPage,
});

const albumRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/album/$provider/$id",
  component: AlbumPage,
});

const artistRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/artist/$provider/$id",
  component: ArtistPage,
});

const playlistRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/playlist/$provider/$id",
  component: PlaylistPage,
});

const downloadsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/downloads",
  // GET /downloads now requires a signed-in user (closed permission gap:
  // it used to expose the whole instance's history to anyone). Guard the
  // page the same way /config already does, instead of letting an
  // anonymous visitor hit a 401 with no explanation.
  component: () => (
    <RequireCapability capability="download">
      <DownloadsPage />
    </RequireCapability>
  ),
  validateSearch: parseDownloadsSearch,
});

// Settings: /config opens the default section, /config/<area> each one.
const guardedConfigPage = () => (
  <RequireCapability capability={["settings_personal", "admin"]}>
    <ConfigPage />
  </RequireCapability>
);
const configRoute = createRoute({ getParentRoute: () => rootRoute, path: "/config", component: guardedConfigPage });
const configSectionRoute = createRoute({ getParentRoute: () => rootRoute, path: "/config/$section", component: guardedConfigPage });

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: () => (
    <OpenModeRedirect>
      <LoginPage />
    </OpenModeRedirect>
  ),
  validateSearch: (search: Record<string, unknown>): { redirect?: string } =>
    typeof search.redirect === "string" ? { redirect: safeRedirect(search.redirect) } : {},
});

// Where an authorization at a provider ends (the server redirects here with a one-time code or an error code).
const loginProviderRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login/provider",
  component: () => (
    <OpenModeRedirect>
      <ProviderLoginReturnPage />
    </OpenModeRedirect>
  ),
  validateSearch: (search: Record<string, unknown>): { code?: string; error?: string } => ({
    ...(typeof search.code === "string" ? { code: search.code } : {}),
    ...(typeof search.error === "string" ? { error: search.error } : {}),
  }),
});

const registerRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/register",
  component: () => (
    <OpenModeRedirect>
      <RegisterPage />
    </OpenModeRedirect>
  ),
  validateSearch: (search: Record<string, unknown>): { redirect?: string } =>
    typeof search.redirect === "string" ? { redirect: safeRedirect(search.redirect) } : {},
});

// The legal notices and the attribution (NOTICE.md): open to every viewer, linked from the navigation.
const aboutRoute = createRoute({ getParentRoute: () => rootRoute, path: "/about", component: AboutPage });

// L'Account è stato assorbito dentro Settings (tab "Account"): /account
// resta come alias storico e reindirizza all'hub unico delle impostazioni.
const accountRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/account",
  beforeLoad: () => {
    throw redirect({ to: "/config" });
  },
});

const favoritesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/favorites",
  // ?view=elsewhere: the likes from the other services that are not in the library.
  validateSearch: (search: Record<string, unknown>): { view?: "elsewhere" } =>
    search.view === "elsewhere" ? { view: "elsewhere" } : {},
  component: function FavoritesRoute() {
    const { view } = favoritesRoute.useSearch();
    const navigate = favoritesRoute.useNavigate();
    return (
      <RequireCapability capability="favorite">
        <FavoritesPage
          view={view ?? "favorites"}
          onViewChange={(next) => void navigate({ search: next === "elsewhere" ? { view: "elsewhere" } : {} })}
        />
      </RequireCapability>
    );
  },
});

// The viewer's own playlists (private): the list, and one playlist.
const playlistsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/playlists",
  component: () => (
    <RequireCapability capability="playlists">
      <PlaylistsPage />
    </RequireCapability>
  ),
});
const userPlaylistRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/playlists/$id",
  component: () => (
    <RequireCapability capability="playlists">
      <UserPlaylistPage />
    </RequireCapability>
  ),
});

// The old addresses of the page (it was "Library", saved tracks).
const legacyLibraryRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/library/saved",
  beforeLoad: () => {
    throw redirect({ to: "/favorites" });
  },
});
const libraryIndexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/library",
  beforeLoad: () => {
    throw redirect({ to: "/favorites" });
  },
});

// Administration: one guarded layout, one child per section.
const adminRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/admin",
  component: () => (
    <RequireCapability capability="admin">
      <AdminLayout />
    </RequireCapability>
  ),
});

const adminIndexRoute = createRoute({
  getParentRoute: () => adminRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/admin/users" });
  },
});

const adminUsersRoute = createRoute({ getParentRoute: () => adminRoute, path: "/users", component: AdminUsersPage });
const adminRequestsRoute = createRoute({ getParentRoute: () => adminRoute, path: "/requests", component: AdminRequestsPage });
const adminProvidersRoute = createRoute({ getParentRoute: () => adminRoute, path: "/providers", component: AdminProvidersPage });
const adminRoutinesRoute = createRoute({ getParentRoute: () => adminRoute, path: "/routines", component: RoutinesPanel });
const adminSourcesRoute = createRoute({ getParentRoute: () => adminRoute, path: "/sources", component: AdminSourcesPage });
const adminDuplicatesRoute = createRoute({ getParentRoute: () => adminRoute, path: "/duplicates", component: AdminDuplicatesPage });
const adminSystemRoute = createRoute({
  getParentRoute: () => adminRoute,
  path: "/system",
  component: () => <SettingsArea area="system" />,
});

const routeTree = rootRoute.addChildren([
  homeRoute,
  searchRoute,
  libraryIndexRoute,
  trackRoute,
  albumRoute,
  artistRoute,
  playlistRoute,
  downloadsRoute,
  favoritesRoute,
  playlistsRoute,
  userPlaylistRoute,
  legacyLibraryRoute,
  configRoute,
  configSectionRoute,
  loginRoute,
  loginProviderRoute,
  registerRoute,
  accountRoute,
  aboutRoute,
  adminRoute.addChildren([
    adminIndexRoute,
    adminUsersRoute,
    adminRequestsRoute,
    adminProvidersRoute,
    adminRoutinesRoute,
    adminSourcesRoute,
    adminDuplicatesRoute,
    adminSystemRoute,
  ]),
]);

export const router = createRouter({
  routeTree,
  // Every route has its own error boundary inside the shell, and an unknown
  // path shows a 404 page instead of a bare "Not Found".
  defaultErrorComponent: RouteError,
  defaultNotFoundComponent: NotFoundPage,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
