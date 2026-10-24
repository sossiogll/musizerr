/**
 * Query keys shared across the app. The first element is the "root": it is
 * what invalidation and persistence match on.
 */

/**
 * Whose credentials a status was asked with. GET /auth/account/status answers
 * for the bearer token, so a status belongs to the viewer it was fetched for:
 * `anonymous` (no credentials: the guest's or the open instance's) or
 * `session` (signed in). The two never share an entry, so a guest status
 * cannot stand in for a session's, saved copy or not.
 */
export type ViewerScope = "anonymous" | "session";

const VIEWER_ROOT = "auth-config";

export const queryKeys = {
  /** Root of the viewer's status in every scope: what invalidation and persistence match on. */
  viewer: [VIEWER_ROOT] as const,
  viewerStatus: (scope: ViewerScope) => [VIEWER_ROOT, scope] as const,
  me: ["me"] as const,
  providersPublic: ["providers-public"] as const,
  /** The providers a user can sign in through (public). */
  loginProviders: ["login-providers"] as const,
  downloadStates: ["download-states"] as const,
  downloads: (params?: Record<string, unknown>) => (params ? (["downloads", params] as const) : (["downloads"] as const)),
  downloadsSummary: ["downloads-summary"] as const,
  favorites: (params?: Record<string, unknown>) => (params ? (["favorites", params] as const) : (["favorites"] as const)),
  likedElsewhere: (params?: Record<string, unknown>) =>
    params ? (["liked-elsewhere", params] as const) : (["liked-elsewhere"] as const),
  /**
   * One library track's favorite, shared by every view of it (rows, pages,
   * the player): written by each change the viewer makes, read over what a
   * list said (see hooks/useTrackFavorite).
   */
  favorite: (libraryId: string) => ["favorite", libraryId] as const,
  libraryJobs: ["library-jobs"] as const,
  /** The viewer's playlists (private: never kept in the offline copy). */
  userPlaylists: ["user-playlists"] as const,
  userPlaylist: (id: string) => ["user-playlist", id] as const,
  /** The viewer's own download requests (approval policy), one page of a status filter. */
  myRequests: (params?: Record<string, unknown>) => (params ? (["my-requests", params] as const) : (["my-requests"] as const)),
  /** Every user's requests, for the admin (one page of the filters). */
  adminRequests: (params?: Record<string, unknown>) => (params ? (["admin-requests", params] as const) : (["admin-requests"] as const)),
  /** The instance's accounts (admin): the Users list, and the requester filter of the Requests list. */
  adminUsers: ["admin-users"] as const,
  /** The number of requests waiting for an admin: the badge. */
  pendingRequestCount: ["pending-request-count"] as const,
  /** The version that runs and the newer releases of Musizeer (admin). */
  updates: ["updates"] as const,
  /** The general download approval policy (admin). */
  downloadPolicy: ["download-policy"] as const,
  /** One user's download policy override and the policy that results from it (admin). */
  userDownloadPolicy: (userId: string) => ["user-download-policy", userId] as const,
  /** The sources of providers, the catalog of each, what is installed and the gateway's state (admin). */
  sources: ["sources"] as const,
  sourceCatalog: (id: string) => ["sources", id, "catalog"] as const,
  installedProviders: ["installed-providers"] as const,
  providerGateway: ["provider-gateway"] as const,
  discover: (section: string) => ["discover", section] as const,
} as const;

/**
 * Roots of the lists whose tracks carry a library state (file state,
 * favorite): marked stale (never refetched on the spot: the rows already
 * show the change) when the viewer downloads or (un)likes a track.
 */
export const TRACK_LIST_ROOTS: readonly string[] = [
  "search",
  "discover",
  "album",
  "playlist",
  "user-playlist",
  "favorites",
  "liked-elsewhere",
  "artist-albums",
  "artist-top-tracks",
  "track",
];

/**
 * Roots kept in the offline copy (lib/query-persistence.ts): the library,
 * the downloads, discover, the public provider labels, the download-state
 * vocabulary and who the viewer is. Nothing admin-only or secret.
 */
export const PERSISTED_QUERY_ROOTS: ReadonlySet<string> = new Set([
  VIEWER_ROOT,
  "me",
  "providers-public",
  "download-states",
  "downloads",
  "downloads-summary",
  "favorites",
  "liked-elsewhere",
  "discover",
]);

/**
 * Roots that say who the viewer is. They are kept (and asked again) when the
 * offline copy changes owner: they are what tells the app the owner changed,
 * and dropping them would cancel the answer that brought the new viewer in.
 */
export const VIEWER_QUERY_ROOTS: ReadonlySet<string> = new Set([VIEWER_ROOT, "me"]);
