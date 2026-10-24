// ------------------------------------------------------------------
// Identificativi
// ------------------------------------------------------------------

export interface ExternalId {
  type: string;
  value: string;
  provider?: string | null;
}

// ------------------------------------------------------------------
// Modelli core — specchio di providers/models.py
// ------------------------------------------------------------------

export interface AlbumRef {
  name: string;
  provider: string;
  provider_id: string;
}

export interface ArtistRef {
  name: string;
  provider: string;
  provider_id: string;
}

/** Tipo esplicito di ogni elemento restituito dall'API. */
export type ItemKind = "track" | "album" | "artist" | "playlist";

export interface Track {
  kind: "track";
  /** Posizione nell'album, se il provider la conosce. */
  track_number?: number | null;
  disc_number?: number | null;
  internal_id: string;
  title: string;
  artist: string;
  album: string;
  albums: AlbumRef[];
  artists: ArtistRef[];
  duration_ms: number;
  cover_url: string | null;
  preview_url: string | null;
  stream_url: string | null;
  provider: string;
  provider_id: string;
  artist_provider_id: string;
  album_provider_id: string;
  year: number | null;
  external_ids: ExternalId[];
  providers: string[];
  /** The FILE state, decided by the server: "downloaded" = in the library (any source). */
  download_status?: DownloadStatus | null;
  /** The file is ours, wherever it is stored (server-decided). */
  in_library?: boolean;
  /** Id of the track in the library database (what the favorites routes take). */
  library_id?: string | null;
  /** One of the viewer's favorites. */
  favorite?: boolean;
  /** A download of the viewer's will add it to their favorites when it arrives. */
  favorite_pending?: boolean;
  /** What the viewer can play, decided by the server. */
  playback?: Playback;
  /** On its own detail response only: providers that failed or ran out of time while cross-enriching it. */
  provider_errors?: ProviderError[];
}

/**
 * What the library state of a track (its file state, the viewer's
 * favorite) is read from: a Track of a list, or the track the player holds.
 */
export type TrackStateFields = Pick<Track, "title" | "provider" | "provider_id"> &
  Partial<
    Pick<Track, "internal_id" | "external_ids" | "library_id" | "download_status" | "in_library" | "favorite" | "favorite_pending">
  >;

/** What the viewer can play of a track: `full` plays `stream_url`, `preview` plays `preview_url`. */
export interface Playback {
  type: "full" | "preview" | "none";
  source: "library" | "provider" | null;
}

/** The viewer's favorites among a collection's tracks in the library. */
export interface FavoritesSummary {
  favorite: number;
  /** In the library, not favorites yet: what "Add to favorites" adds. */
  addable: number;
}

export interface Album {
  kind: "album";
  internal_id: string;
  title: string;
  artist: string;
  year: number | null;
  track_count: number | null;
  cover_url: string | null;
  provider: string;
  provider_id: string;
  artist_provider_id: string;
  artists: ArtistRef[];
  external_ids: ExternalId[];
  tracks: Track[];
  providers: string[];
  album_type: AlbumType | null;
  /** Solo nella discografia di un artista: ogni edizione della stessa uscita. */
  editions?: AlbumEdition[];
  /** Nel dettaglio, nella discografia e sulle schede della Home: tracce in libreria, in corso, fallite. */
  library?: LibraryCoverage;
  /** Nel dettaglio: i preferiti di chi guarda fra le tracce in libreria. */
  favorites?: FavoritesSummary;
  /** Nel dettaglio: se il confronto con gli altri provider è andato a buon fine. */
  enrichment?: { complete: boolean; reason: "provider_error" | "track_error" | null };
  /** On its own detail response only: providers that failed or ran out of time while cross-enriching it. */
  provider_errors?: ProviderError[];
}

/** Copertura della libreria calcolata dal server. */
export interface LibraryCoverage {
  /** Tracks of the collection; null when no source knows it ("N in library"). */
  total: number | null;
  downloaded: number;
  in_progress: number;
  failed: number;
}

export type AlbumType = "album" | "single" | "ep" | "compilation" | "live" | "appears_on";

export interface AlbumEdition {
  provider: string;
  provider_id: string;
  title: string;
  year: number | null;
  album_type: AlbumType | null;
  track_count: number | null;
  cover_url: string | null;
  external_ids: ExternalId[];
}

export interface Artist {
  kind: "artist";
  internal_id: string;
  name: string;
  provider: string;
  provider_id: string;
  cover_url: string | null;
  external_ids: ExternalId[];
  providers: string[];
  /** On its own detail response only: providers that failed or ran out of time while cross-enriching it. */
  provider_errors?: ProviderError[];
  /** On its own detail response only: the provider can give the whole discography to download (the `discography` capability). */
  discography_downloadable?: boolean;
}

/** Perché le tracce di una raccolta non si possono leggere. */
export interface TracksUnavailable {
  code: "access_restricted" | "error";
  message: string;
}

export interface Playlist {
  kind: "playlist";
  internal_id: string;
  name: string;
  provider: string;
  provider_id: string;
  description: string;
  cover_url: string | null;
  tracks: Track[];
  /** Tracce dichiarate dal provider, se le conosce. */
  track_count: number | null;
  /** Presente quando le tracce non si possono leggere. */
  tracks_unavailable: TracksUnavailable | null;
  library?: LibraryCoverage;
  favorites?: FavoritesSummary;
  external_ids: ExternalId[];
}

export interface LrcLine {
  time_ms: number;
  text: string;
}

export interface Lyrics {
  plain: string | null;
  synced: LrcLine[];
  source: string;
}

export type SearchResult = Track | Album | Artist | Playlist;
export type SearchType = "track" | "album" | "artist" | "playlist";

// ------------------------------------------------------------------
// Standard paginated envelope — all list endpoints use this shape
// ------------------------------------------------------------------

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  limit: number;
  pages: number;
}

export interface SearchResponse extends PaginatedResponse<SearchResult>, WithProviderErrors {
  type: SearchType;
  query: string;
}

// ------------------------------------------------------------------
// Download / Queue
// ------------------------------------------------------------------

// Vocabolario canonico allineato a core/download_states.py
export type DownloadStatus =
  | "pending"
  | "downloading"
  | "importing"
  | "conflict"
  | "retrying"
  | "dispatched"
  | "downloaded"
  | "failed";

export interface DownloadAttempt {
  download_id: string;
  status: DownloadStatus;
  retry_count: number;
  provider_used: string | null;
  file_path: string | null;
  error_code: string | null;
  error_message: string | null;
  /** Testo grezzo dell'errore, solo per gli admin. */
  error_detail?: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface TrackDownload {
  track_id: string;
  status: DownloadStatus;
  provider_used: string | null;
  file_path: string | null;
  requested_by: string | null;
  track: Track | null;
  attempts: DownloadAttempt[];
}

/** What a download request did (POST /download/track, /downloads/{id}/retry). */
export type DownloadRequestStatus = "queued" | "attached" | "already_in_library";

export interface DownloadRequestResult {
  /** `already_in_library`: nothing was queued (`download_id` null). */
  status: DownloadRequestStatus;
  download_id: string | null;
  /** The track's id in the library database. */
  library_id: string;
}

export interface DownloadResponse extends DownloadRequestResult {
  track: Track;
}

/**
 * The 202 answer of a download route when the request waits for an admin (the
 * approval policy): nothing was queued, delegated or marked as a favorite.
 */
export interface PendingApprovalResponse {
  status: "pending_approval";
  request_id: string;
  kind: RequestKind;
  /** The tracks that need a download (not the ones in the library or on their way). */
  needed_tracks: number;
  reason: WaitingReason;
  /** Artist: releases whose tracks could not be read, so they are missing from the request. */
  releases_failed?: number;
}

// ------------------------------------------------------------------
// Download approval policy (core/download_policy.py, services/download_requests.py)
// ------------------------------------------------------------------

/** What was asked for. `liked_elsewhere` is the library job "download the ones liked elsewhere". */
export type RequestKind = "track" | "album" | "playlist" | "artist" | "liked_elsewhere";
export type RequestStatus = "pending" | "approved" | "denied" | "cancelled";
/** `auto`: approved at once; `approval`: always waits; `auto_up_to`: at once up to a threshold of tracks. */
export type RequestMode = "auto" | "approval" | "auto_up_to";
/** Why a request waits for an admin. */
export type WaitingReason = "kind_requires_approval" | "over_threshold" | "over_quota";
/** Why a request was approved at once, or why it waits. */
export type RequestReason = "auto" | "within_threshold" | WaitingReason;

/** A download request of a user the approval policy governs (GET /download-requests). */
export interface DownloadRequest {
  id: string;
  kind: RequestKind;
  status: RequestStatus;
  /** Approved by the policy, not by an admin. */
  auto: boolean;
  reason: RequestReason;
  /** What was asked for (empty strings for `liked_elsewhere`). */
  target: { provider: string; id: string };
  title: string;
  /** The artist of a track or album. */
  subtitle: string;
  cover_url: string | null;
  /** The tracks that needed a download when it was decided. */
  needed_tracks: number;
  /** The new downloads an approved request queued (null while pending, denied or cancelled). */
  queued_tracks: number | null;
  dispatched_to: string | null;
  add_to_favorites: boolean;
  /** The admin's note on an approval, or the reason of a denial. */
  decision_note: string | null;
  created_at: string;
  decided_at: string | null;
}

/** A request as an admin sees it (GET /admin/download-requests): who asked and who decided. */
export interface AdminDownloadRequest extends DownloadRequest {
  requested_by: { id: string; username: string | null };
  /** The admin who decided (the requester, for a cancellation). */
  decided_by: { id: string; username: string | null } | null;
}

/** How a decision on one request ended (POST /admin/download-requests/approve|deny). */
export type DecisionOutcome = "decided" | "unchanged" | "conflict" | "not_found" | "failed";

export interface DecisionResult {
  id: string;
  outcome: DecisionOutcome;
  request: AdminDownloadRequest | null;
}

/** POST /download-requests/{id}/cancel */
export interface DownloadRequestChange {
  request: DownloadRequest;
  /** False when it was already in the state asked for. */
  changed: boolean;
}

/**
 * A download request on the progress stream. To its requester (with
 * `user_id`): it was approved or denied. To the admins (no `user_id`, with
 * `pending`, the number of requests now waiting): one is waiting, or was
 * decided or cancelled.
 */
export interface DownloadRequestEvent {
  type: "download_request";
  event: RequestStatus;
  user_id?: string;
  pending?: number;
  request: DownloadRequest | AdminDownloadRequest;
}

/** What a download request of the viewer would meet right now (GET /auth/account/status). */
export interface ViewerDownloadRule {
  mode: RequestMode;
  threshold: number;
  /**
   * The most tracks that need a download a request of this kind can have and
   * still be approved at once right now, the quota left included: null = no
   * limit, 0 = it always waits.
   */
  auto_up_to: number | null;
}

export interface ViewerDownloadPolicy {
  /** False for an admin and with authentication off: `rules` and `quota` are then null. */
  applies: boolean;
  rules: Record<RequestKind, ViewerDownloadRule> | null;
  quota: { tracks: number; window_days: number; used: number; remaining: number | null } | null;
}

/** One kind's rule of a policy. */
export interface DownloadPolicyRule {
  mode: RequestMode;
  threshold: number;
}

/** A download approval policy: the rule of each kind of request and the per-user quota. */
export interface DownloadPolicy {
  rules: Record<RequestKind, DownloadPolicyRule>;
  quota: { tracks: number; window_days: number };
}

/** What an admin changed, for one user: a field that is missing or null inherits the general policy. */
export interface UserDownloadPolicyOverride {
  rules: Partial<Record<RequestKind, { mode?: RequestMode | null; threshold?: number | null }>>;
  quota_tracks: number | null;
}

/** GET|PUT|DELETE /admin/users/{id}/download-policy */
export interface UserDownloadPolicyView {
  override: UserDownloadPolicyOverride;
  effective: DownloadPolicy;
}

export interface BulkDownloadResponse {
  download_ids: string[];
  /** Tracks queued. */
  total_tracks: number;
  /** Album/playlist with only_missing: tracks left out, already in the library. */
  skipped?: number;
  /** Album: handed to an album-level download provider instead. */
  dispatched_to?: string;
  /** Artist: releases whose tracks could not be read, so they are missing from the request. */
  releases_failed?: number;
}

// Evento SSE progress
export type DownloadStep = "starting" | "enriching" | "downloading";

export interface ProgressEvent {
  /** Set only on events that are not a download (e.g. "library_job"). */
  type?: undefined;
  download_id: string;
  status: DownloadStatus;
  track_title: string;
  track_id?: string;
  /**
   * The FILE state of the track after this event, decided by the server: what
   * a row shows (`status` is only this attempt's). `downloaded` for a track in
   * the library, whatever happens to the attempt.
   */
  file_state?: DownloadStatus | null;
  phase?: string | null;
  step?: DownloadStep;
  provider_attempting?: string;
  method?: "id" | "fuzzy";
  retry?: number;
}

/** Any event of the progress stream. */
export type StreamEvent = ProgressEvent | LibraryJobEvent | DownloadRequestEvent;

// ------------------------------------------------------------------
// Provider
// ------------------------------------------------------------------

export type AuthFlow = "none" | "oauth2" | "api_key" | "credentials" | "external_utility";

export interface AccountDisplayField {
  key: string;
  label: string;
}

export interface ExternalUtilityAccount {
  name: string;
  [key: string]: string;
}

/** Whose account a provider is used with, declared by the provider (core/auth.py::AccountModel). */
export type AccountModel = "none" | "personal" | "personal_with_shared" | "system";

/** The account a connect/test/disconnect is about: the viewer's own, or the instance's shared one (admins). */
export type AccountTarget = "personal" | "shared";

/** One field of the login form of a `credentials` account; a `secret` one is never shown back. */
export interface CredentialField {
  key: string;
  label: string;
  secret: boolean;
}

/** The viewer's own account of a provider. */
export interface PersonalAccountState {
  connected: boolean;
  username: string | null;
  /** Capabilities (sdk/src/musizeer_sdk/capabilities.py) the own connection was not granted the permission for: reconnecting grants them. */
  missing_permissions: string[];
  /** What an own account enables, declared by the provider. */
  capabilities: string[];
  /**
   * Whether the viewer can sign in through this account: null when the provider cannot sign anybody in,
   * false when it can and this account carries no id from it yet (reconnect it).
   */
  login: boolean | null;
}

/** The instance's shared account of a provider; `username` is for admins only. */
export interface SharedAccountState {
  connected: boolean;
  username: string | null;
}

/** GET /auth/linked-accounts: a provider the viewer may connect their own account to. */
export interface LinkedAccount {
  name: string;
  label: string;
  auth_flow: "oauth2" | "credentials";
  account_model: AccountModel;
  /** False until an admin has set up what a connection needs (the OAuth2 app). */
  connectable: boolean;
  credential_fields: CredentialField[];
  personal: PersonalAccountState;
  /** Present when the instance has an account that serves users who have none. */
  shared: SharedAccountState | null;
}

/** GET /auth/account/login-providers: a provider a user can sign in through. */
export interface LoginProvider {
  name: string;
  label: string;
  /** `oauth2`: the browser goes to the provider; `credentials`: the form in `credential_fields` is posted. */
  auth_flow: "oauth2" | "credentials";
  credential_fields: CredentialField[];
}

export interface ProviderInfo {
  name: string;
  label: string;
  auth_flow: AuthFlow;
  account_model: AccountModel;
  /** The viewer's own account; null when the model has no personal accounts. */
  personal: PersonalAccountState | null;
  /** The instance's shared account; null when the model has none. */
  shared: SharedAccountState | null;
  /** The fields of the login form (`credentials` providers). */
  credential_fields?: CredentialField[];
  /** Whether the viewer's own calls authenticate (the account their model routes them to). */
  authenticated: boolean;
  // Veritiero anche quando auth_flow è "none" (nessun login da tentare):
  // combina `authenticated` con la completezza della config (`config_valid`)
  // e, per i provider senza credenziali proprie, con la loro disponibilità
  // reale. `authenticated` da solo mentiva in quel caso — vedi the admin providers page.
  usable: boolean;
  config_valid?: boolean;
  client_id?: string;
  // external_utility extras
  accounts?: ExternalUtilityAccount[];
  account_display_fields?: AccountDisplayField[];
  borrowed_oauth_client_from?: string;
  borrowed_oauth_client_ok?: boolean;
  library_available?: boolean;
  library_package?: string;
  library_url?: string;
  disclaimer?: string;
  setup_command?: string;
  setup_instructions?: string;
  config: Record<string, unknown>;
  // Fase 5c, Task 4 (fix): altre categorie in cui QUESTO STESSO oggetto
  // provider (identità lato backend, mai il `name`) è registrato — vuoto per
  // un provider a ruolo singolo. Il `name` da solo NON basta a dedurre
  // l'identità: due CLASSI DISTINTE nel registry possono condividere un nome
  // pur essendo provider indipendenti (visto dal vivo, metadata
  // e download separati) — vedi the admin providers page.
  also_registered_in?: string[];
  // Metadata providers only: the optional capabilities it declares
  // (sdk/src/musizeer_sdk/capabilities.py).
  capabilities?: string[];
}

export interface ProvidersResponse {
  metadata: ProviderInfo[];
  download: ProviderInfo[];
  lyrics: ProviderInfo[];
  storage: ProviderInfo[];
  sync: ProviderInfo[];
  // Fase 5a fix 2: GET /providers ora copre ogni categoria che il registry
  // conosce (derivata da core/settings_resolver.py::PROVIDER_CATEGORY_GETTERS),
  // "identification" compresa — prima assente da questa risposta pur avendo
  // impostazioni proprie nello schema unificato.
  identification: ProviderInfo[];
}

// ------------------------------------------------------------------
// Sources
// ------------------------------------------------------------------

export interface ProviderSource {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  added_at: string;
  /** A token is set. The token itself never reaches the browser. */
  has_credential: boolean;
  /** The gateway may speak plain HTTP to the image registry of this source's providers. */
  allow_insecure_registry: boolean;
}

export interface AddSourceInput {
  url: string;
  credential?: string;
  allow_insecure_registry?: boolean;
}

/** Only the fields present change; `credential: null` removes the token. */
export interface EditSourceInput {
  enabled?: boolean;
  credential?: string | null;
  allow_insecure_registry?: boolean;
}

export interface FilesystemAccess {
  setting: string;
  mode: "ro" | "rw";
}

/** Raw TCP to the host the setting names, on "any" port, one port ("2121") or a range ("30000-30100"). */
export interface TcpAccess {
  setting: string;
  ports: string;
}

/** What a provider's sandbox is granted: the only resources it will have. */
export interface ProviderPermissions {
  /** Fixed destinations, host:port; the host may be a wildcard (*.example.com = any subdomain, not example.com itself). */
  egress: string[];
  /** Settings whose value (a URL or host the user types) becomes reachable. */
  egress_from_settings: string[];
  filesystem_from_settings: FilesystemAccess[];
  /** Settings whose value names a host the provider may open raw TCP connections to (a protocol that is not HTTP). */
  tcp_from_settings: TcpAccess[];
  limits: { memory_mb?: number | null; open_files?: number | null; processes?: number | null };
}

export interface AddedRemoved<T = string> {
  added: T[];
  removed: T[];
}

/** What an update changes in what the admin approved. */
export interface PermissionsDiff {
  egress: AddedRemoved;
  egress_from_settings: AddedRemoved;
  filesystem_from_settings: AddedRemoved<FilesystemAccess>;
  tcp_from_settings: AddedRemoved<TcpAccess>;
  limits: { name: string; old: number | null; new: number | null }[];
  roles: AddedRemoved;
  is_empty: boolean;
  /** It asks for something new: a host, a path, a role or a larger limit. */
  grants_more: boolean;
}

export interface CatalogEntry {
  name: string;
  label: string;
  description: string;
  homepage: string | null;
  /** registry/repository, no tag. */
  image: string;
  /** The registry host the gateway pulls from (and sends the source's token to, if it has one). */
  registry: string;
  digest: string;
  provider_api_version: number;
  roles: string[];
  permissions: ProviderPermissions;
  /** It implements this Musizeer's provider API version; if not, it cannot be installed. */
  compatible: boolean;
  incompatibility: string | null;
}

export interface ProviderUpdateOffer extends CatalogEntry {
  diff: PermissionsDiff;
}

export interface SourceCatalogEntry extends CatalogEntry {
  installed: boolean;
  installed_from_this_source: boolean;
  installed_digest: string | null;
  update: ProviderUpdateOffer | null;
}

export interface SourceCatalog {
  source: ProviderSource;
  name: string;
  entries: SourceCatalogEntry[];
  /** Entries that are not valid, with the reason. */
  rejected: { name: string; reason: string }[];
}

export interface InstalledProvider {
  name: string;
  label: string;
  image: string;
  registry: string;
  digest: string;
  provider_api_version: number;
  roles: string[];
  /** What the admin approved. */
  permissions: ProviderPermissions;
  source_id: string;
  source_name: string | null;
  /** The gateway uses the source's token to pull this image. */
  sends_source_token: boolean;
  approved_at: string;
  /** The admin who approved it; null when approved from the host's shell. */
  approved_by: string | null;
  installed_at: string;
  /** What its source's catalog offers over it; null when nothing newer. */
  update?: ProviderUpdateOffer | null;
  /** Why an update could not be looked at (a catalog error code, `source_disabled`...). */
  update_error?: string | null;
}

export type GatewayProviderStateName =
  | "pulling"
  | "starting"
  | "running"
  | "restarting"
  | "refused"
  | "unavailable"
  | "failed"
  | "stopped";

export interface GatewayProviderState {
  name: string;
  state: GatewayProviderStateName;
  reason: string;
  restarts: number;
  roles: string[];
}

export interface ProviderGatewayState {
  reachable: boolean;
  status: "ok" | "degraded" | "unavailable" | "unreachable";
  detail: string;
  deployed: boolean;
  sandbox: { available: boolean | null; reason: string } | null;
  providers: GatewayProviderState[];
  /** Entries of the pushed set the gateway refused, by name. */
  rejected: Record<string, string>;
  /** Providers the gateway runs that this Musizeer refused to register, by name. */
  host_rejected: Record<string, string>;
}

// ------------------------------------------------------------------
// Playback
// ------------------------------------------------------------------

export type PlaybackType = "preview" | "full";

// ------------------------------------------------------------------
// Library / Saved Tracks
// ------------------------------------------------------------------

export interface ProviderSavedInfo {
  added_at: string | null;
}

/** A favorite of the user: always a track in the library. */
export interface FavoriteTrack extends Track {
  favorited_at: string;
}

/** Liked on another service, not in the library. */
export interface LikedElsewhereTrack extends Track {
  saved_at: string;
  provider_saved_at: Record<string, ProviderSavedInfo>;
}

export interface FavoriteChange {
  track_id: string;
  favorite: boolean;
  changed: boolean;
}

export type LibraryJobKind = "library_sync" | "download_sync";
export type LibraryJobStatus = "never_run" | "queued" | "running" | "completed" | "failed";
/** Generic phase keys (see services/library_jobs.py). */
export type LibraryJobPhase = "fetching" | "saving" | "preparing" | "checking";

export interface LibraryJobProvider {
  provider: string;
  status: "queued" | "running" | "completed" | "failed";
  processed: number;
  /** null while unknown (e.g. while the likes are read). */
  total?: number | null;
  count: number;
  error_code?: string | null;
  error_message?: string | null;
  /** Raw error text: admins only. */
  error_detail?: string;
}

/**
 * One run of a library job ("Sync likes", "Download the ones liked
 * elsewhere"), as GET /library/jobs and the progress stream give it. The
 * server decides its state; only kind and status exist before the first run.
 */
export interface LibraryJobRun {
  kind: LibraryJobKind;
  status: LibraryJobStatus;
  id?: string;
  task_id?: string | null;
  params?: Record<string, unknown>;
  phase?: LibraryJobPhase | null;
  provider?: string | null;
  processed?: number;
  /** null = not known (indeterminate progress). */
  total?: number | null;
  providers?: LibraryJobProvider[];
  /**
   * library_sync {tracks_synced}; download_sync {total, checked, queued,
   * errors}, plus `pending_approval` when the likes download is a request that
   * waits for an admin (nothing was queued).
   */
  result?: LibraryJobResult;
  queued_at?: string | null;
  started_at?: string | null;
  heartbeat_at?: string | null;
  completed_at?: string | null;
  /** Stable code (e.g. "worker_unavailable"), translated by the UI. */
  error_code?: string | null;
  /** Readable English message. */
  error_message?: string | null;
  /** Raw error text: admins only. */
  error_detail?: string;
}

/** The counts a finished run reports, and the request a likes download left waiting. */
export interface LibraryJobResult {
  /** library_sync */
  tracks_synced?: number;
  /** download_sync */
  total?: number;
  checked?: number;
  queued?: number;
  errors?: number;
  skipped_removed?: number;
  pending_approval?: { request_id: string; needed_tracks: number; reason: WaitingReason };
}

export type LibraryJobs = Record<LibraryJobKind, LibraryJobRun>;

/** POST /library/sync and /library/download-sync: the new run, or the one already active. */
export type LibraryJobStarted = LibraryJobRun & { created: boolean };

/** A library job's progress on the progress stream (sent to its owner only). */
export interface LibraryJobEvent {
  type: "library_job";
  user_id: string;
  run: LibraryJobRun;
}


// ------------------------------------------------------------------
// History
// ------------------------------------------------------------------

export type DownloadsResponse = PaginatedResponse<TrackDownload>;

// ------------------------------------------------------------------
// Auth
// ------------------------------------------------------------------

export type UserRole = "admin" | "user";

export interface User {
  id: string;
  username: string;
  email: string | null;
  role: UserRole;
  is_active: boolean;
  /** Created by a sign-in through a provider and waiting for an administrator to activate it (admin lists only). */
  pending_approval?: boolean;
}

/** Cosa può fare chi sta guardando, calcolato dal server (GET /auth/account/status). */
export type ViewerMode = "open" | "guest" | "user" | "admin";

export interface ViewerCapabilities {
  search: boolean;
  /** Download and add to favorites when it arrives. */
  download: boolean;
  /** Download without adding to favorites (admins, auth disabled). */
  download_plain: boolean;
  /** The heart on a track in the library. */
  favorite: boolean;
  stream_full: boolean;
  library: boolean;
  /** The viewer's own playlists (GET /playlists): private, kept by Musizeer. */
  playlists: boolean;
  settings_personal: boolean;
  /** Connect your own provider accounts (GET /auth/linked-accounts). */
  linked_accounts: boolean;
  admin: boolean;
}

/** GET /providers/public: etichette dei provider e ordine dei metadati, senza login. */
export interface PublicProviders {
  providers: { id: string; label: string }[];
  /** The viewer's own priority when signed in and set, else the system one. */
  metadata_priority: string[];
  /** Every metadata provider, in that priority order. */
  metadata: string[];
  /** The optional capability keys (sdk/src/musizeer_sdk/capabilities.py) each metadata provider declares, by provider id. */
  metadata_capabilities: Record<string, string[]>;
}

export interface AuthConfig {
  auth_enabled: boolean;
  registration_open: boolean;
  mode: ViewerMode;
  capabilities: ViewerCapabilities;
  /** What a download request would meet right now; null for a guest, who cannot download. */
  download_policy: ViewerDownloadPolicy | null;
  /** Whether downloads can work: the working folder and the library folder are set (ADR-0008); null for a guest. */
  folders?: ViewerFolders | null;
}

/** One folder the downloads need. `setting` and `detail` (why it is not set) are for administrators. */
export interface ViewerFolder {
  set: boolean;
  state: "set" | "not_set" | "root_missing" | "root_refused" | "invalid";
  /** False for the library folder when no storage provider is active or its library is not a folder. */
  applicable: boolean;
  setting?: string | null;
  detail?: string;
}

export interface ViewerFolders {
  ready: boolean;
  missing: ("working_folder" | "library_folder")[];
  working_folder: ViewerFolder;
  library_folder: ViewerFolder;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

/** A provider that failed while an aggregated response was built. */
export interface ProviderError {
  provider: string;
  error_code: string;
  error_message: string;
}

/** Aggregated routes return what succeeded and name the providers that failed. */
export interface WithProviderErrors {
  provider_errors: ProviderError[];
}

/** GET /track/{provider}/{id}/related */
export interface RelatedTracksResponse extends WithProviderErrors {
  items: Track[];
}

/** GET /track/{provider}/{id}/appears-on: un'uscita che contiene la stessa registrazione. */
export interface AppearsOnRelease {
  title: string;
  year: number | null;
  provider: string;
  provider_id: string;
  track_provider_id: string;
}

export interface AppearsOnResponse extends WithProviderErrors {
  items: AppearsOnRelease[];
}

/** GET /artist/{provider}/{id}/top-tracks: `error_code` when every provider failed. */
export interface TopTracksResponse extends WithProviderErrors {
  items: Track[];
  error_code: string | null;
  error_message: string | null;
}

/** GET /artist/{provider}/{id}/albums: one entry per release, with the artist's library summary. */
export interface ArtistAlbumsResponse extends PaginatedResponse<Album>, WithProviderErrors {
  library: LibraryCoverage;
  /** The provider's listing reached the bound the server reads: the artist may have more releases. */
  may_be_longer: boolean;
}

/** GET /artist/{provider}/{id}/top-tracks */
export interface ArtistTopTracksResponse extends WithProviderErrors {
  items: Track[];
  /** Valorizzati solo se tutti i provider interrogati sono falliti. */
  error_code: string | null;
  error_message: string | null;
}

// ------------------------------------------------------------------
// The viewer's own playlists (services/playlists.py, docs/playlists.md)
// ------------------------------------------------------------------

/** `own` is edited by its owner; `followed` mirrors a playlist of a metadata provider (read-only). */
export type UserPlaylistKind = "own" | "followed";

/** A playlist of the viewer, without its tracks (GET /playlists). */
export interface UserPlaylist {
  id: string;
  name: string;
  description: string;
  kind: UserPlaylistKind;
  /** Followed playlists: where it comes from. */
  source_provider: string | null;
  source_id: string | null;
  cover_url: string | null;
  track_count: number;
  /** Followed playlists: the tracks its source gains are downloaded. */
  auto_download: boolean;
  /** The provider would not give this followed playlist's tracks. */
  tracks_unavailable: TracksUnavailable | null;
  /** Why the last look at the source failed. */
  refresh_error_code: string | null;
  checked_at: string | null;
  created_at: string;
  updated_at: string;
  /** The sync providers that hold a copy on the viewer's own account. */
  synced_to: { sync_provider: string; synced_at: string }[];
}

export interface UserPlaylistTrack extends Track {
  added_at: string;
}

export interface UserPlaylistDetail extends UserPlaylist {
  tracks: UserPlaylistTrack[];
}

export interface UserPlaylistList {
  items: UserPlaylist[];
  /** The most tracks a playlist may hold (the admin's cap). */
  max_tracks: number;
}

/** What a request to download a playlist's tracks came to. */
export interface PlaylistDownload {
  status: "queued" | "pending_approval" | "none" | "error";
  queued?: number;
  skipped?: number;
  request_id?: string;
  kind?: RequestKind;
  needed_tracks?: number;
  reason?: WaitingReason;
}

export interface FollowResult {
  playlist: UserPlaylist;
  created: boolean;
  download: PlaylistDownload;
}

export interface PlaylistRefreshResult {
  playlist: UserPlaylist;
  changed: boolean;
  new_tracks: number;
  removed_tracks: number;
  error_code: string | null;
  download: PlaylistDownload;
}

/** A track to put in a playlist: one the host knows, or one a provider names. */
export type PlaylistTrackRef = { track_id: string } | { provider: string; provider_id: string };

export interface AddPlaylistTracksResult {
  added: number;
  skipped_duplicates: number;
  playlist: UserPlaylist;
}
