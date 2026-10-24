import axios, { AxiosError, type InternalAxiosRequestConfig } from "axios";
import type {
  AccountTarget,
  AdminDownloadRequest,
  Album,
  AlbumEdition,
  AppearsOnResponse,
  Artist,
  ArtistAlbumsResponse,
  AuthConfig,
  AddPlaylistTracksResult,
  BulkDownloadResponse,
  DecisionResult,
  DownloadPolicy,
  DownloadRequest,
  DownloadRequestChange,
  DownloadRequestResult,
  DownloadResponse,
  DownloadStatus,
  DownloadsResponse,
  LibraryJobs,
  LibraryJobStarted,
  LinkedAccount,
  LoginProvider,
  Lyrics,
  PaginatedResponse,
  PlaylistRefreshResult,
  PlaylistTrackRef,
  PendingApprovalResponse,
  PublicProviders,
  AddSourceInput,
  EditSourceInput,
  InstalledProvider,
  ProviderGatewayState,
  ProviderSource,
  SourceCatalog,
  ProvidersResponse,
  RelatedTracksResponse,
  RequestKind,
  RequestStatus,
  FavoriteChange,
  FollowResult,
  FavoriteTrack,
  LikedElsewhereTrack,
  SearchResponse,
  SearchType,
  TokenResponse,
  TopTracksResponse,
  Track,
  User,
  UserDownloadPolicyOverride,
  UserDownloadPolicyView,
  UserPlaylist,
  UserPlaylistDetail,
  UserPlaylistList,
  UserRole,
  WithProviderErrors,
} from "@/types";
import { authStore } from "./auth-store";
import { classifyError } from "./api-errors";
import { reportUnreachable } from "./connectivity";

/**
 * A request path with every interpolated value URL-encoded as one segment:
 * an id containing "/", "?" or ".." can never reach another endpoint.
 */
export function path(strings: TemplateStringsArray, ...values: (string | number)[]): string {
  return strings.reduce((out, part, i) => out + part + (i < values.length ? encodeURIComponent(String(values[i])) : ""), "");
}


// In dev Vite proxia /api → http://localhost:8000 (vite.config.ts)
// In prod nginx proxia /api → http://app:8000
const BASE = "/api";

/** A request that gets no answer within this time counts as unreachable. */
export const REQUEST_TIMEOUT_MS = 15_000;

const http = axios.create({
  baseURL: BASE,
  withCredentials: true,
  timeout: REQUEST_TIMEOUT_MS,
  paramsSerializer: {
    serialize: (params) => {
      const parts: string[] = [];
      for (const [key, value] of Object.entries(params)) {
        if (Array.isArray(value)) {
          value.forEach((v) => parts.push(`${key}=${v}`));
        } else if (value !== undefined && value !== null) {
          parts.push(`${key}=${encodeURIComponent(String(value))}`);
        }
      }
      return parts.join("&");
    },
  },
});

// ------------------------------------------------------------------
// Auth interceptors — inject Bearer + 401 → refresh → retry
// ------------------------------------------------------------------

type RetriableConfig = InternalAxiosRequestConfig & { _retry?: boolean };

const isAuthPath = (url?: string) =>
  !!url && /\/auth\/account\/(login|register|refresh|logout|status)\b/.test(url);

http.interceptors.request.use((config) => {
  const token = authStore.getAccessToken();
  if (token && config.headers) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

let refreshInFlight: Promise<string | null> | null = null;

type RefreshOutcome =
  | { kind: "ok"; accessToken: string }
  /** No token, a stale session, or an unreachable server: nothing to do. */
  | { kind: "none" }
  /** The token sent was refused but another tab has replaced it since. */
  | { kind: "superseded" };

/**
 * One refresh round trip, under the cross-tab lock. The token is read AFTER
 * the lock is acquired: the tab that held it before may have rotated it.
 */
function refreshOnce(generation: number): Promise<RefreshOutcome> {
  return authStore.withRefreshLock(async (): Promise<RefreshOutcome> => {
    const sent = authStore.getRefreshToken();
    if (!sent || authStore.getGeneration() !== generation) return { kind: "none" };
    try {
      const { data } = await axios.post<TokenResponse>(
        `${BASE}/auth/account/refresh`,
        { refresh_token: sent },
        { timeout: REQUEST_TIMEOUT_MS },
      );
      if (authStore.getGeneration() !== generation) return { kind: "none" };
      authStore.setAccessToken(data.access_token);
      authStore.setRefreshToken(data.refresh_token);
      return { kind: "ok", accessToken: data.access_token };
    } catch (error) {
      // An unreachable server says nothing about the session: keep the
      // refresh token and try again when the API is back. Signing the user
      // out here would turn every outage into a forced sign-out.
      const { kind } = classifyError(error);
      if (kind === "unreachable" || kind === "upstream") {
        if (kind === "unreachable") reportUnreachable();
        return { kind: "none" };
      }
      // Refused. It says the session is over only if the token refused is
      // still the one the session holds: another tab (without Web Locks, or
      // a sign-in there) may have replaced it while this request was out.
      const current = authStore.getRefreshToken();
      if (current !== null && current !== sent) return { kind: "superseded" };
      // Only clear if this is still the session that failed — a stale
      // refresh must not tear down a session that signed back in after it
      // started. A LOCAL-only logout (the refresh token itself is what just
      // failed): the streaming cookie goes too, or it would outlive the
      // session it was issued for by up to its own TTL.
      if (authStore.getGeneration() === generation) {
        authStore.clear();
        endStreamSession();
      }
      return { kind: "none" };
    }
  });
}

async function refreshAccessToken(): Promise<string | null> {
  if (refreshInFlight) return refreshInFlight;
  // Captured synchronously, before any await: this is the session this
  // refresh belongs to. If authStore.clear() (sign-out) or beginSession()
  // (sign-in) runs while the request is in flight, the generation will have
  // moved on by the time we come back, and we must not write tokens for a
  // session that no longer exists.
  const generation = authStore.getGeneration();
  const attempt = (async (): Promise<string | null> => {
    let outcome = await refreshOnce(generation);
    // Once more with the token the other tab left: it is the session's now.
    if (outcome.kind === "superseded") outcome = await refreshOnce(generation);
    if (outcome.kind !== "ok") return null;
    // Awaited (not fire-and-forget): every caller of refreshAccessToken —
    // including the AuthContext bootstrap effect on page load — treats a
    // resolved token as "authenticated", and PlayerContext trusts that to
    // pick stream_url over preview_url. If the cookie weren't guaranteed
    // set by the time this resolves, a fast reload could establish
    // "authenticated" before the cookie exists and 401 on first play.
    // Outside the lock: it does not touch the refresh token.
    await syncStreamSession(outcome.accessToken);
    return outcome.accessToken;
  })();
  // Cleared when THIS attempt settles. (A `finally` inside the async body
  // would run synchronously when it returns before its first await — no
  // refresh token — i.e. before the assignment below, leaving a settled
  // attempt stuck here and every later refresh answering null.)
  refreshInFlight = attempt;
  void attempt.finally(() => {
    if (refreshInFlight === attempt) refreshInFlight = null;
  });
  return attempt;
}

http.interceptors.response.use(
  (r) => r,
  async (error: AxiosError) => {
    if (classifyError(error).kind === "unreachable") reportUnreachable();
    const original = error.config as RetriableConfig | undefined;
    if (!original || original._retry || error.response?.status !== 401) {
      return Promise.reject(error);
    }
    if (isAuthPath(original.url)) {
      return Promise.reject(error);
    }
    original._retry = true;
    const token = await refreshAccessToken();
    if (!token) return Promise.reject(error);
    if (original.headers) original.headers.Authorization = `Bearer ${token}`;
    return http(original);
  },
);

export { refreshAccessToken };

// ------------------------------------------------------------------
// Stream session — HttpOnly cookie that authorizes the full-audio routes
// (routes/stream.py) for the <audio> element, which cannot carry a bearer
// header. Synced after login and after every access-token refresh so it
// stays valid as long as the signed-in session does (docs/permissions.md).
// Uses bare axios (not `http`) so a failure here never re-enters the
// refresh/retry interceptor above.
// ------------------------------------------------------------------

function syncStreamSession(accessToken: string): Promise<void> {
  return axios
    .post(`${BASE}/auth/account/stream-session`, null, {
      withCredentials: true,
      headers: { Authorization: `Bearer ${accessToken}` },
    })
    .then(() => undefined)
    .catch(() => {
      // Non fatale: senza cookie il player ricade sulla preview.
    });
}

export const endStreamSession = () =>
  axios
    .delete(`${BASE}/auth/account/stream-session`, { withCredentials: true })
    .catch(() => {});

// ------------------------------------------------------------------
// Auth — user login/register/me
// ------------------------------------------------------------------

export const getAuthConfig = () =>
  http.get<AuthConfig>("/auth/account/status").then((r) => r.data);

export const login = (username: string, password: string) =>
  http
    .post<TokenResponse>("/auth/account/login", { username, password })
    .then(async (r) => {
      await syncStreamSession(r.data.access_token);
      return r.data;
    });

/** The providers a user can sign in through (public, from what each declares). */
export const getLoginProviders = () =>
  http.get<{ providers: LoginProvider[] }>("/auth/account/login-providers").then((r) => r.data.providers);

/** Where the browser goes to sign in through an OAuth2 provider: the server redirects it to the provider. */
export const providerLoginStartUrl = (provider: string) => `${BASE}${path`/auth/account/login/${provider}/start`}`;

/** Sign in with a provider's login account: `fields` are its `credential_fields`. */
export const loginWithProviderCredentials = (provider: string, fields: Record<string, string>) =>
  http
    .post<TokenResponse>(path`/auth/account/login/${provider}/credentials`, fields)
    .then(async (r) => {
      await syncStreamSession(r.data.access_token);
      return r.data;
    });

/** The session the one-time code of a sign-in through a provider is worth (it works once). */
export const exchangeLoginCode = (code: string) =>
  http
    .post<TokenResponse>("/auth/account/login/exchange", { code })
    .then(async (r) => {
      await syncStreamSession(r.data.access_token);
      return r.data;
    });

export const register = (username: string, password: string, email?: string) =>
  http
    .post<User>("/auth/account/register", { username, password, ...(email ? { email } : {}) })
    .then((r) => r.data);

export const logoutRequest = (refresh_token: string) =>
  http.post("/auth/account/logout", { refresh_token });

export const getMe = () => http.get<User>("/auth/account/me").then((r) => r.data);

// ------------------------------------------------------------------
// Admin — user management
// ------------------------------------------------------------------

export const adminListUsers = () =>
  http.get<PaginatedResponse<User>>("/admin/users").then((r) => r.data);

/** POST /admin/users: a new, active account (works with authentication on and off). */
export interface NewAccountBody {
  username: string;
  password: string;
  email?: string;
  role: UserRole;
}

export const adminCreateUser = (body: NewAccountBody) =>
  http.post<User>("/admin/users", body).then((r) => r.data);

export const adminUpdateUser = (
  id: string,
  updates: { role?: UserRole; is_active?: boolean },
) => http.patch<User>(path`/admin/users/${id}`, updates).then((r) => r.data);

export const adminDeleteUser = (id: string) =>
  http.delete(path`/admin/users/${id}`).then((r) => r.data);

// ------------------------------------------------------------------
// Search
// ------------------------------------------------------------------

export const search = (q: string, type: SearchType, providers?: string[]) =>
  http
    .get<SearchResponse>("/search", { params: { q, type, ...(providers?.length ? { providers } : {}) } })
    .then((r) => r.data);

// ------------------------------------------------------------------
// Discover
// ------------------------------------------------------------------

export const getNewReleases = (limit = 20) =>
  http.get<{ items: Album[] } & WithProviderErrors>("/discover/new-releases", { params: { limit } }).then((r) => r.data);

export const getTrendingAlbums = (limit = 20) =>
  http.get<{ items: Album[] } & WithProviderErrors>("/discover/trending/albums", { params: { limit } }).then((r) => r.data);

export const getTrendingTracks = (limit = 20) =>
  http.get<{ items: Track[] } & WithProviderErrors>("/discover/trending/tracks", { params: { limit } }).then((r) => r.data);

export const getTrendingArtists = (limit = 20) =>
  http.get<{ items: Artist[] } & WithProviderErrors>("/discover/trending/artists", { params: { limit } }).then((r) => r.data);

// ------------------------------------------------------------------
// Content — fetch metadati
// ------------------------------------------------------------------

export const getTrack = (provider: string, id: string) =>
  http.get(path`/track/${provider}/${id}`).then((r) => r.data);

export const getAlbum = (provider: string, id: string) =>
  http.get(path`/album/${provider}/${id}`).then((r) => r.data);

export const getArtist = (provider: string, id: string) =>
  http.get(path`/artist/${provider}/${id}`).then((r) => r.data);

export const getArtistAlbums = (provider: string, id: string) =>
  http
    .get<ArtistAlbumsResponse>(path`/artist/${provider}/${id}/albums`)
    .then((r) => r.data);

/** The editions of the album's release on its provider (the album included). */
export const getAlbumEditions = (provider: string, id: string) =>
  http.get<{ items: AlbumEdition[] }>(path`/album/${provider}/${id}/editions`).then((r) => r.data);

/** The artist's most popular tracks; `error_code` when every provider failed. */
export const getArtistTopTracks = (provider: string, id: string, limit = 10) =>
  http
    .get<TopTracksResponse>(
      path`/artist/${provider}/${id}/top-tracks`,
      { params: { limit } },
    )
    .then((r) => r.data);

/** Variants of the track, then similar tracks; empty when no provider offers them. */
export const getRelatedTracks = (provider: string, id: string, limit = 20) =>
  http.get<RelatedTracksResponse>(path`/track/${provider}/${id}/related`, { params: { limit } }).then((r) => r.data);

/** Releases containing the same recording (same ISRC or MBID). */
export const getAppearsOn = (provider: string, id: string) =>
  http.get<AppearsOnResponse>(path`/track/${provider}/${id}/appears-on`).then((r) => r.data);

export const getPlaylist = (provider: string, id: string) =>
  http.get(path`/playlist/${provider}/${id}`).then((r) => r.data);

export const getTrackLyrics = (provider: string, id: string) =>
  http.get<Lyrics>(path`/track/${provider}/${id}/lyrics`).then((r) => r.data);

// ------------------------------------------------------------------
// Download
// ------------------------------------------------------------------

/**
 * `addToFavorites`: the tracks join the viewer's favorites when they arrive.
 * Only an admin chooses: a user's download always does (the server forces it).
 */
export interface DownloadOptions {
  addToFavorites?: boolean;
  /** Album, playlist: the server skips the tracks in the library or on their way. */
  onlyMissing?: boolean;
}

export const downloadTrack = (
  provider: string,
  id: string,
  external_ids?: { type: string; value: string }[],
  { addToFavorites }: DownloadOptions = {},
) =>
  http
    .post<DownloadResponse | PendingApprovalResponse>("/download/track", { provider, id, external_ids, add_to_favorites: addToFavorites })
    .then((r) => r.data);

const downloadCollection = (kind: "album" | "playlist" | "artist") =>
  (provider: string, id: string, { onlyMissing = false, addToFavorites }: DownloadOptions = {}) =>
    http
      .post<BulkDownloadResponse | PendingApprovalResponse>(`/download/${kind}`, {
        provider,
        id,
        only_missing: onlyMissing,
        add_to_favorites: addToFavorites,
      })
      .then((r) => r.data);

export const downloadAlbum = downloadCollection("album");
export const downloadPlaylist = downloadCollection("playlist");
export const downloadArtist = downloadCollection("artist");

// ------------------------------------------------------------------
// Downloads attivi + history
// ------------------------------------------------------------------

export interface DownloadsQuery {
  page?: number;
  limit?: number;
  /** Aggregated-state phases (any of them). */
  phase?: DownloadPhase[];
  q?: string;
  sort?: "recent" | "title" | "artist";
}

export const getDownloads = ({ page = 1, limit = 50, phase, q, sort }: DownloadsQuery = {}) =>
  http
    .get<DownloadsResponse>("/downloads", { params: { page, limit, phase, q: q || undefined, sort } })
    .then((r) => r.data);

/** GET /downloads/summary: tracks per aggregated state and phase. */
export interface DownloadsSummary {
  statuses: Record<DownloadStatus, number>;
  phases: Record<DownloadPhase, number>;
  total: number;
  /** Tracks that landed in the library in the last `days` days. */
  downloaded_since: number;
}

export const getDownloadsSummary = (days = 7) =>
  http.get<DownloadsSummary>("/downloads/summary", { params: { days } }).then((r) => r.data);

export const retryFailedDownloads = ({ addToFavorites }: DownloadOptions = {}) =>
  http
    .post<{ download_ids: string[] }>("/downloads/retry-failed", null, { params: { add_to_favorites: addToFavorites } })
    .then((r) => r.data);

export const retryDownload = (trackId: string, { addToFavorites }: DownloadOptions = {}) =>
  http
    .post<DownloadRequestResult>(path`/downloads/${trackId}/retry`, null, { params: { add_to_favorites: addToFavorites } })
    .then((r) => r.data);

// ------------------------------------------------------------------
// Download requests — the approval policy
// ------------------------------------------------------------------

export type RequestsQuery = {
  page?: number;
  limit?: number;
  /** Repeatable: only requests in one of these states. */
  status?: RequestStatus[];
};

export type AdminRequestsQuery = RequestsQuery & {
  kind?: RequestKind[];
  /** Only this requester's requests. */
  userId?: string;
};

/** GET /download-requests: the viewer's own requests, newest first. */
export const getMyRequests = ({ page = 1, limit = 25, status }: RequestsQuery = {}) =>
  http.get<PaginatedResponse<DownloadRequest>>("/download-requests", { params: { page, limit, status } }).then((r) => r.data);

/** POST /download-requests/{id}/cancel: cancels the viewer's own pending request. */
export const cancelMyRequest = (id: string) =>
  http.post<DownloadRequestChange>(path`/download-requests/${id}/cancel`).then((r) => r.data);

/** GET /admin/download-requests: everybody's requests, with who asked and who decided. */
export const getAdminRequests = ({ page = 1, limit = 25, status, kind, userId }: AdminRequestsQuery = {}) =>
  http
    .get<PaginatedResponse<AdminDownloadRequest>>("/admin/download-requests", { params: { page, limit, status, kind, user_id: userId } })
    .then((r) => r.data);

/** GET /admin/download-requests/pending-count: how many wait for an admin (the badge). */
export const getPendingRequestCount = () =>
  http.get<{ pending: number }>("/admin/download-requests/pending-count").then((r) => r.data);

/** Approves many pending requests (one id is a single approval): one outcome per id. */
export const approveRequests = (ids: string[], note?: string) =>
  http.post<{ results: DecisionResult[] }>("/admin/download-requests/approve", { ids, note: note || null }).then((r) => r.data.results);

/** Denies many pending requests: one outcome per id. */
export const denyRequests = (ids: string[], reason?: string) =>
  http.post<{ results: DecisionResult[] }>("/admin/download-requests/deny", { ids, reason: reason || null }).then((r) => r.data.results);

/** GET /admin/download-policy: the general policy (the `download_requests` settings). */
export const getDownloadPolicy = () => http.get<DownloadPolicy>("/admin/download-policy").then((r) => r.data);

export const getUserDownloadPolicy = (userId: string) =>
  http.get<UserDownloadPolicyView>(path`/admin/users/${userId}/download-policy`).then((r) => r.data);

/** Replaces a user's override (what is missing or null follows the general policy). */
export const setUserDownloadPolicy = (userId: string, override: UserDownloadPolicyOverride) =>
  http.put<UserDownloadPolicyView>(path`/admin/users/${userId}/download-policy`, override).then((r) => r.data);

/** The user follows the general policy again. */
export const clearUserDownloadPolicy = (userId: string) =>
  http.delete<UserDownloadPolicyView>(path`/admin/users/${userId}/download-policy`).then((r) => r.data);

// ------------------------------------------------------------------
// Auth provider
// ------------------------------------------------------------------

export const saveApiKey = (provider: string, api_key: string) =>
  http.post(path`/auth/${provider}/apikey`, { api_key }).then((r) => r.data);

export const saveOAuthClient = (
  provider: string,
  client_id: string,
  client_secret: string,
) =>
  http
    .post(path`/auth/${provider}/client`, { client_id, client_secret })
    .then((r) => r.data);

/** Connect a login account: `fields` are the provider's `credential_fields`. */
export const saveCredentials = (provider: string, fields: Record<string, string>, target?: AccountTarget) =>
  http
    .post(path`/auth/${provider}/credentials`, fields, { params: target ? { target } : {} })
    .then((r) => r.data);

export const deleteOAuthClient = (provider: string) =>
  http.delete(path`/auth/${provider}/client`).then((r) => r.data);

/** Disconnect an account: the viewer's own, or (admin) the shared one. */
export const revokeAuth = (provider: string, target?: AccountTarget) =>
  http.delete(path`/auth/${provider}`, { params: target ? { target } : {} }).then((r) => r.data);

export const revokeAllAuth = () =>
  http.delete<{ ok: boolean; removed: number }>("/auth/me/accounts").then((r) => r.data);

/** `scope` "app": the OAuth2 app client (admin). "account" (default): an account, the viewer's routed one unless `target` says. */
export const testProviderConnection = (provider: string, scope?: "app" | "account", target?: AccountTarget) =>
  http.post(path`/auth/${provider}/test`, null, { params: { ...(scope ? { scope } : {}), ...(target ? { target } : {}) } })
    .then((r) => r.data as {
      app: { ok: boolean; detail: string } | null;
      account: { ok: boolean; detail: string } | null;
    });

export const getOAuthAuthorizeUrl = (provider: string, target?: AccountTarget) =>
  http.get<{ url: string }>(path`/auth/${provider}/authorize-url`, { params: target ? { target } : {} }).then((r) => r.data.url);

/** The providers the viewer may connect their own account to, with what is connected. */
export const getLinkedAccounts = () =>
  http.get<{ accounts: LinkedAccount[] }>("/auth/linked-accounts").then((r) => r.data.accounts);

// ------------------------------------------------------------------
// Settings — schema/values/write unificati (core + provider), filtrati per
// ruolo dal backend. GET /config, PATCH /config, GET /config/schema e
// PATCH /providers/{name}/config (il percorso legacy) sono stati rimossi:
// questo è l'unico percorso rimasto.
// ------------------------------------------------------------------

export interface SettingDef {
  key: string;
  type: "bool" | "int" | "float" | "string" | "enum" | "list" | "provider_ref";
  group: string;
  area: string;
  label: string;
  description: string;
  /** Translation keys of label and description; the text above is the fallback. */
  label_key?: string;
  description_key?: string;
  /** The provider capability (ProviderInfo.capabilities) the setting acts
   * through: the UI lists the providers that declare it. */
  provider_capability?: string | null;
  admin_only: boolean;
  // Governance dichiarata (core/settings_catalog.py::SettingScope).
  scope?: "system" | "user" | "system_with_user_override";
  options: string[] | null;
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  secret: boolean;
  provider_category?: string | null;
  required?: boolean;
  visible_when?: [string, unknown][] | null;
  // Il verdetto dei requisiti dichiarati (`requires`) contro lo stato
  // attuale; `unavailable_reason` è già una stringa leggibile costruita dal
  // backend (categoria/label dichiarate, mai un nome di provider), da
  // mostrare così com'è — nessuna traduzione o ricostruzione lato frontend.
  available?: boolean;
  unavailable_reason?: string | null;
  // True for a setting that has its own dedicated page (e.g. the system
  // routines, see the "routines" group) — the GENERIC settings UI must skip
  // it (see SettingsArea.settingsForArea); it is still a full SettingDef
  // (schema/values/write all go through the same declarative pipeline), it
  // just isn't rendered there.
  hidden?: boolean;
  /** Translation key of each option, in the order of `options`. */
  option_label_keys?: string[] | null;
  /** Choosing `value` asks for an explicit confirmation first. */
  confirm?: { value: unknown; message_key: string; message: string } | null;
}

// Metadati dichiarativi di area/gruppo (label, descrizione, ordine di
// visualizzazione) — la stessa idea delle label di provider lette da
// GET /providers, applicata alla tassonomia delle impostazioni. Il frontend
// non tiene una propria mappa hardcoded: legge queste da qui.
export interface SettingsAreaMeta {
  key: string;
  /** Chiave stabile per la traduzione (es. "settings.areas.library"); label è il default. */
  label_key: string;
  label: string;
}

export interface SettingsGroupMeta {
  /** Translation key of the description (settings.group_descriptions.<key>). */
  description_key: string;
  key: string;
  label_key: string;
  label: string;
  description: string;
  area: string;
}

export interface SettingsTaxonomy {
  areas: SettingsAreaMeta[];
  groups: SettingsGroupMeta[];
}

export interface ConfigSchemaResponse {
  settings: SettingDef[];
  taxonomy: SettingsTaxonomy;
}

export const getSettingsSchema = () =>
  http.get<ConfigSchemaResponse>("/settings/schema").then((r) => r.data);

export const getSettingsValues = () =>
  http.get<Record<string, unknown>>("/settings/values").then((r) => r.data);

/**
 * PATCH /settings. `adoptInto`: the administrator who takes the open-mode owner's data when this
 * write turns authentication on (the server defaults to the oldest one that can sign in).
 */
export const patchSettings = (updates: Record<string, unknown>, options: { adoptInto?: string } = {}) =>
  http
    .patch<Record<string, unknown>>("/settings", updates, options.adoptInto ? { params: { adopt_into: options.adoptInto } } : undefined)
    .then((r) => r.data);

export type DownloadTone = "neutral" | "info" | "success" | "warning" | "danger";
export type DownloadPhase = "queued" | "active" | "done" | "failed" | "review";

/** GET /config/download-states: one entry per state, in canonical order. */
export interface DownloadStateMeta {
  key: DownloadStatus;
  /** Fallback text; the UI translates label_key. */
  label: string;
  label_key: string;
  tone: DownloadTone;
  phase: DownloadPhase;
}

export const getDownloadStates = () =>
  http.get<{ states: DownloadStateMeta[] }>("/config/download-states").then((r) => r.data.states);

export const startFullSyncVerify = () =>
  http.post<{ task_id: string }>("/config/verify-full-sync").then((r) => r.data);

export const getFullSyncVerifyStatus = (taskId: string) =>
  http
    .get<{ ready: boolean; result: { status: string; full_sync?: boolean; detail?: string } | null }>(
      path`/config/verify-full-sync/${taskId}`,
    )
    .then((r) => r.data);

// ------------------------------------------------------------------
// System routines (admin) — GET/PATCH/run of the periodic background jobs
// declared in app/workers/routines.py (RoutineDef). Their enabled/interval
// settings ALSO exist in the generic schema (group "routines", hidden:
// true) so they go through the same validated write path, but this
// dedicated page is what actually shows/edits them (see RoutinesPanel).
// ------------------------------------------------------------------

export interface RoutineRunInfo {
  /** When it was dispatched (put in the queue). */
  queued_at: string;
  /** When a worker really started it; null while it waits in the queue. */
  started_at: string | null;
  finished_at: string | null;
  /** How long it waited in the queue; null until it starts. */
  wait_seconds: number | null;
  /** How long it executed (the wait excluded); null until it finishes. */
  duration_seconds: number | null;
  /** queued: waiting for a worker; skipped: its period was already covered. */
  status: "queued" | "running" | "ok" | "error" | "skipped";
  /** What a successful run returned, as data: rendered per routine. */
  result: Record<string, unknown> | null;
  /** A successful run's result that is not an object, as text. */
  detail: string | null;
  /** Codice stabile per una run fallita (es. "routine_unreachable", "sync_provider_unreachable"). */
  error_code: string | null;
  error_message?: string;
  error_detail?: string | null;
  /** Where a running run is (routines that report progress); null otherwise. */
  progress?: RoutineRunProgress | null;
}

/** The progress of a running routine: the same keys as a library job's. */
export interface RoutineRunProgress {
  /** Generic phase key, named by the UI (routines.phases.<phase>). */
  phase: string;
  provider: string | null;
  processed: number;
  total: number | null;
  /** When the run last reported. */
  heartbeat_at: string | null;
}

/**
 * One provider's report inside a routine's result (`result.providers`, e.g.
 * the library index: one per sync provider). `status` is "done" or how it
 * failed; a failure carries its stable error code.
 */
export interface RoutineProviderReport {
  sync_provider?: string;
  provider?: string;
  status: string;
  error_code?: string | null;
  error_message?: string | null;
  error_detail?: string | null;
  [count: string]: unknown;
}

export interface RoutineInfo {
  key: string;
  task: string;
  /** The routine whose end starts this one; null when only its period does. */
  follows?: string | null;
  enabled: boolean;
  interval_minutes: number;
  min_interval_minutes: number;
  last_run: RoutineRunInfo | null;
  next_run_at: string | null;
  /** A one-time job that has finished: nothing schedules it again (null next run), "Run now" still runs it. */
  done?: boolean;
  /** A run waits in the queue for a worker. */
  queued: boolean;
  /** A run is executing now. */
  running: boolean;
  /** La prossima esecuzione è scaduta da oltre il margine: worker o beat fermi. */
  scheduler_stale: boolean;
  /** Work the routine set going still in progress; null when it leaves none. */
  active_work: number | null;
}

export const getRoutines = () =>
  http.get<{ routines: RoutineInfo[] }>("/system/routines").then((r) => r.data.routines);

export const patchRoutine = (key: string, updates: { enabled?: boolean; interval_minutes?: number }) =>
  http.patch<{ status: string }>(path`/system/routines/${key}`, updates).then((r) => r.data);

export const runRoutineNow = (key: string) =>
  http.post<{ run_id: string }>(path`/system/routines/${key}/run`).then((r) => r.data);

/** Why an update needs more than pulling its images (core/versions.py). */
export interface UpdateReason {
  code: "major" | "upgrade_path" | "contract_unsupported" | "contract_migrations" | "manual_steps" | "no_metadata";
  /** upgrade_path: the version to go through first. */
  through?: string;
  /** contract_unsupported: the installed providers the new version would not run. */
  providers?: string[];
}

export interface AvailableUpdate {
  version: string;
  url: string | null;
  published_at: string | null;
  kind: "major" | "minor" | "patch";
  /** Pulling the new images is all it takes. */
  plain: boolean;
  reasons: UpdateReason[];
}

export interface SchemaStatus {
  /** The Alembic revision(s) the database is at, "+"-joined. */
  revision: string;
  /** The version that last migrated it; null for a database migrated by hand. */
  migrated_by: string | null;
  min_app_version: string | null;
  contract_revision: string | null;
  phase: "expand" | "contract" | null;
  migrated_at: string | null;
}

export interface UpdatesInfo {
  current: string;
  commit: string;
  /** The schema the database is at and what migrated it (ADR-0007); null when it cannot be read. */
  schema: SchemaStatus | null;
  /** A development build: nothing to compare. */
  development: boolean;
  check_enabled: boolean;
  checked_at: string | null;
  last_check_failed: boolean;
  /** Why the releases could not be read at the last check that answered; not a failure of the instance. */
  release_info_unavailable: "not_found" | "unauthorized" | "rate_limited" | "unreachable" | "upstream_error" | null;
  /** Newest first. */
  updates: AvailableUpdate[];
  latest: string | null;
}

/** GET /admin/updates: the version that runs and the newer releases of Musizerr (admin, ADR-0007). */
export const getUpdates = () => http.get<UpdatesInfo>("/admin/updates").then((r) => r.data);

/**
 * One download provider's registration of the push callback (POST /delegation/register-webhook): registered,
 * not supported (the provider does not implement it) or failed, with the classified `error_code` and the
 * provider's own reason in `error_detail` (the route is admin-only).
 */
export interface DelegationWebhookRegistrationResult {
  provider: string;
  label: string;
  ok: boolean;
  state: "registered" | "not_supported" | "failed";
  detail: string;
  error_code?: string;
  error_message?: string;
  error_detail?: string;
}

export const registerDelegationWebhooks = () =>
  http
    .post<{ results: DelegationWebhookRegistrationResult[] }>("/delegation/register-webhook")
    .then((r) => r.data.results);

/** Provider labels and the viewer's metadata priority: readable by anyone. */
export const getPublicProviders = () =>
  http.get<PublicProviders>("/providers/public").then((r) => r.data);

export const getProviders = () =>
  http.get<ProvidersResponse>("/providers").then((r) => r.data);

// ------------------------------------------------------------------
// Provider accounts (external utility auth)
// ------------------------------------------------------------------

export const deleteProviderAccount = (providerName: string, accountName: string) =>
  http.delete(path`/providers/${providerName}/accounts/${accountName}`).then((r) => r.data);

// ------------------------------------------------------------------
// Sources
// ------------------------------------------------------------------

export const getSources = () =>
  http.get<ProviderSource[]>("/sources").then((r) => r.data);

export const addSource = (input: AddSourceInput) =>
  http.post<ProviderSource>("/sources", input).then((r) => r.data);

export const editSource = (id: string, changes: EditSourceInput) =>
  http.patch<ProviderSource>(path`/sources/${id}`, changes).then((r) => r.data);

/** Removes a source and uninstalls the providers installed from it (their names are the answer). */
export const removeSource = (id: string) =>
  http.delete<{ ok: true; uninstalled: string[] }>(path`/sources/${id}`).then((r) => r.data);

export const getSourceCatalog = (id: string) =>
  http.get<SourceCatalog>(path`/sources/${id}/catalog`).then((r) => r.data);

export const getInstalledProviders = () =>
  http.get<InstalledProvider[]>("/sources/providers/installed").then((r) => r.data);

export const getProviderGatewayState = () =>
  http.get<ProviderGatewayState>("/sources/providers/gateway").then((r) => r.data);

/** `digest` is the one the admin was shown: the server refuses (`catalog_changed`) if the catalog offers another. */
export const installProvider = (source_id: string, provider_name: string, digest: string) =>
  http.post<InstalledProvider>("/sources/providers/install", { source_id, provider_name, digest }).then((r) => r.data);

export const updateProvider = (name: string, digest: string) =>
  http.post<InstalledProvider>(path`/sources/providers/${name}/update`, { digest }).then((r) => r.data);

export const uninstallProvider = (name: string) =>
  http.delete(path`/sources/providers/${name}`).then((r) => r.data);

// ------------------------------------------------------------------
// Favorites and the likes imported from other services
// ------------------------------------------------------------------

export const getFavorites = (page = 1, limit = 50, q?: string, sort?: string) =>
  http
    .get<PaginatedResponse<FavoriteTrack>>("/favorites", { params: { page, limit, q, sort } })
    .then((r) => r.data);

export const getLikedElsewhere = (page = 1, limit = 50, provider?: string, q?: string, sort?: string) =>
  http
    .get<PaginatedResponse<LikedElsewhereTrack>>("/favorites/elsewhere", { params: { page, limit, provider, q, sort } })
    .then((r) => r.data);

export const getFavorite = (libraryId: string) =>
  http
    .get<{ track_id: string; favorite: boolean; in_library: boolean }>(path`/favorites/${libraryId}`)
    .then((r) => r.data);

export const addFavorite = (libraryId: string) =>
  http.put<FavoriteChange>(path`/favorites/${libraryId}`).then((r) => r.data);

export const removeFavorite = (libraryId: string) =>
  http.delete<FavoriteChange>(path`/favorites/${libraryId}`).then((r) => r.data);

/** Favorites the tracks of the list that are in the library; the others come back in `skipped`. */
export const addFavorites = (libraryIds: string[]) =>
  http
    .post<{ added: string[]; skipped: string[] }>("/favorites/batch", { track_ids: libraryIds })
    .then((r) => r.data);

/** Starts the likes sync, or returns the run already queued or running (created false). */
export const triggerLibrarySync = () => http.post<LibraryJobStarted>("/library/sync").then((r) => r.data);

export const deleteSavedTracks = () =>
  http.delete<{ ok: boolean }>("/library/saved-tracks").then((r) => r.data);

/** The latest run of every library job of the viewer. */
export const getLibraryJobs = () => http.get<LibraryJobs>("/library/jobs").then((r) => r.data);

/** Starts "download the ones liked elsewhere", or returns the run already active. */
export const triggerLibraryDownloadSync = (timeRange: "all" | "1m" | "3m") =>
  http
    .post<LibraryJobStarted>("/library/download-sync", null, {
      params: { time_range: timeRange },
    })
    .then((r) => r.data);

// ------------------------------------------------------------------
// The viewer's own playlists
// ------------------------------------------------------------------

export const getUserPlaylists = () => http.get<UserPlaylistList>("/playlists").then((r) => r.data);

export const getUserPlaylist = (id: string) => http.get<UserPlaylistDetail>(path`/playlists/${id}`).then((r) => r.data);

export const createUserPlaylist = (name: string, description = "") =>
  http.post<UserPlaylist>("/playlists", { name, description }).then((r) => r.data);

export const updateUserPlaylist = (id: string, changes: { name?: string; description?: string; auto_download?: boolean }) =>
  http.patch<UserPlaylist>(path`/playlists/${id}`, changes).then((r) => r.data);

/**
 * Deletes an own playlist or unfollows a followed one. By default only the
 * playlist goes; `deleteTracks` also deletes the files that were downloaded for
 * it (the ones nothing else holds), in the background.
 */
export const deleteUserPlaylist = (id: string, { deleteTracks = false }: { deleteTracks?: boolean } = {}) =>
  http
    .delete<{ deleted: boolean; tracks_scheduled: number }>(path`/playlists/${id}`, { params: { delete_tracks: deleteTracks } })
    .then((r) => r.data);

export const addPlaylistTracks = (id: string, items: PlaylistTrackRef[]) =>
  http.post<AddPlaylistTracksResult>(path`/playlists/${id}/tracks`, { items }).then((r) => r.data);

export const removePlaylistTrack = (id: string, trackId: string) =>
  http.delete<{ track_id: string; removed: boolean }>(path`/playlists/${id}/tracks/${trackId}`).then((r) => r.data);

export const reorderPlaylistTracks = (id: string, trackIds: string[]) =>
  http.put<{ track_ids: string[] }>(path`/playlists/${id}/tracks/order`, { track_ids: trackIds }).then((r) => r.data);

export const duplicateUserPlaylist = (id: string, name?: string) =>
  http.post<UserPlaylist>(path`/playlists/${id}/duplicate`, name ? { name } : {}).then((r) => r.data);

/** Follows a playlist of a metadata provider: keeps its tracks and asks to download them (never as favorites). */
export const followPlaylist = (provider: string, id: string) =>
  http.post<FollowResult>("/playlists/follow", { provider, id }).then((r) => r.data);

export const refreshUserPlaylist = (id: string) =>
  http.post<PlaylistRefreshResult>(path`/playlists/${id}/refresh`).then((r) => r.data);

// ------------------------------------------------------------------
// Library duplicates (admin) — groups found by the duplicates scan
// ------------------------------------------------------------------

export type DuplicateGroupStatus = "proposed" | "needs_review" | "resolved" | "dismissed";

export interface DuplicateMember {
  id: string;
  sync_id: string;
  /** The path the sync provider shows: often virtual, two files can share it. */
  path: string;
  /** Where the file really is on the storage, when Musizerr knows it. */
  storage_path: string | null;
  /** When the Musizerr download of this file landed. */
  added_at: string | null;
  /** The download provider that brought it. */
  downloaded_via: string | null;
  title: string;
  artist: string;
  album: string;
  year: number | null;
  duration_ms: number | null;
  size: number | null;
  suffix: string | null;
  bitrate: number | null;
  sample_rate: number | null;
  lossless: boolean | null;
  quality_known: boolean;
  /** Its quality beats every other file of the group (a hint, never a choice). */
  best_quality: boolean;
  /** Plays THIS file (admin); null once deleted. */
  stream_url: string | null;
  fingerprint_score: number | null;
  /** "musizerr" when this app put the file there; only those can be deleted. */
  origin: string;
  deletable: boolean;
  is_keeper: boolean;
  deleted_at: string | null;
}

export interface DuplicateGroup {
  id: string;
  recording_id: string;
  status: DuplicateGroupStatus;
  reason: string | null;
  created_at: string;
  updated_at: string;
  resolved_at: string | null;
  deleted_paths: string[];
  members: DuplicateMember[];
}

export const getDuplicateGroups = (status?: DuplicateGroupStatus) =>
  http
    .get<{ groups: DuplicateGroup[] }>("/library/duplicates", { params: { status } })
    .then((r) => r.data.groups);

export const resolveDuplicateGroup = (groupId: string, keepMemberId: string) =>
  http
    .post<{ group_id: string; status: string; deleted: string[]; skipped_non_deletable: string[] }>(
      path`/library/duplicates/${groupId}/resolve`,
      { keep_member_id: keepMemberId },
    )
    .then((r) => r.data);

export const resolveAllDuplicateGroups = () =>
  http.post<{ resolved: unknown[] }>("/library/duplicates/resolve-all").then((r) => r.data);

export const dismissDuplicateGroup = (groupId: string) =>
  http
    .post<{ group_id: string; status: string }>(path`/library/duplicates/${groupId}/dismiss`)
    .then((r) => r.data);

// ------------------------------------------------------------------
// SSE progress stream
// ------------------------------------------------------------------

export const createProgressSource = () =>
  new EventSource(`${BASE}/progress`);

export default http;
