import type { AuthConfig, RequestKind, ViewerDownloadPolicy, ViewerDownloadRule, ViewerMode } from "@/types";

const KINDS: RequestKind[] = ["track", "album", "playlist", "artist", "liked_elsewhere"];

function rules(each: (kind: RequestKind) => ViewerDownloadRule): NonNullable<ViewerDownloadPolicy["rules"]> {
  return Object.fromEntries(KINDS.map((kind) => [kind, each(kind)])) as NonNullable<ViewerDownloadPolicy["rules"]>;
}

/** `download_policy` of an admin and of open mode: the policy does not apply (services/download_requests.policy_status). */
export const NOT_GOVERNED: ViewerDownloadPolicy = { applies: false, rules: null, quota: null };

/**
 * A user the policy governs but lets download everything at once: every kind
 * `auto`, no quota (`auto_up_to` null = no limit). What `viewerStatus("user")`
 * answers unless a test asks for a policy.
 */
export const AUTOMATIC_POLICY: ViewerDownloadPolicy = {
  applies: true,
  rules: rules(() => ({ mode: "auto", threshold: 30, auto_up_to: null })),
  quota: { tracks: 0, window_days: 7, used: 0, remaining: null },
};

/**
 * The defaults of the backend (core/download_policy.py) for a user who has
 * used nothing of the quota yet: a track `auto`, an album `auto_up_to` 30, a
 * playlist and an artist `approval`, the likes `auto_up_to` 50, a quota of 200
 * tracks per 7 days — which is the limit `auto_up_to` reports for `auto`.
 */
export const DEFAULT_POLICY: ViewerDownloadPolicy = {
  applies: true,
  rules: {
    track: { mode: "auto", threshold: 1, auto_up_to: 200 },
    album: { mode: "auto_up_to", threshold: 30, auto_up_to: 30 },
    playlist: { mode: "approval", threshold: 30, auto_up_to: 0 },
    artist: { mode: "approval", threshold: 30, auto_up_to: 0 },
    liked_elsewhere: { mode: "auto_up_to", threshold: 50, auto_up_to: 50 },
  },
  quota: { tracks: 200, window_days: 7, used: 0, remaining: 200 },
};

/**
 * What GET /auth/account/status answers in each mode, mirroring
 * services/capabilities.py — for tests that mock useAuth(). A user's
 * `download_policy` is the one given (every kind automatic by default).
 */
export function viewerStatus(mode: ViewerMode, downloadPolicy: ViewerDownloadPolicy = AUTOMATIC_POLICY): AuthConfig {
  const signedIn = mode !== "guest";
  return {
    auth_enabled: mode !== "open",
    registration_open: true,
    mode,
    capabilities: {
      search: signedIn,
      download: signedIn,
      download_plain: mode === "admin" || mode === "open",
      favorite: signedIn,
      stream_full: signedIn,
      library: signedIn,
      playlists: signedIn,
      settings_personal: mode === "user" || mode === "admin",
      linked_accounts: signedIn,
      admin: mode === "admin" || mode === "open",
    },
    download_policy: mode === "guest" ? null : mode === "user" ? downloadPolicy : NOT_GOVERNED,
  };
}
