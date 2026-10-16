/**
 * What the approval-policy routes answer, in the shape the API sends them
 * (`app/openapi.yaml`: DownloadRequest, AdminDownloadRequest,
 * DownloadPendingApproval, DownloadRequestEvent, the batch decisions and the
 * policy views; the values follow `app/tests/test_download_requests.py`).
 * Datetimes are UTC with their offset (`core.utc.utc_iso`).
 */
import type {
  AdminDownloadRequest,
  DecisionResult,
  DownloadPolicy,
  DownloadRequest,
  DownloadRequestEvent,
  PendingApprovalResponse,
  UserDownloadPolicyView,
} from "@/types";

/** A request that waits (a user's playlist), as GET /download-requests lists it. */
export const waitingPlaylist: DownloadRequest = {
  id: "req-playlist",
  kind: "playlist",
  status: "pending",
  auto: false,
  reason: "kind_requires_approval",
  target: { provider: "catalog", id: "playlist-259" },
  title: "Rap 2010",
  subtitle: "",
  cover_url: null,
  needed_tracks: 259,
  queued_tracks: null,
  dispatched_to: null,
  add_to_favorites: true,
  decision_note: null,
  created_at: "2026-10-04T08:00:00+00:00",
  decided_at: null,
};

/** An album over the threshold, waiting. */
export const waitingAlbum: DownloadRequest = {
  ...waitingPlaylist,
  id: "req-album",
  kind: "album",
  reason: "over_threshold",
  target: { provider: "catalog", id: "album-31" },
  title: "Box Set",
  subtitle: "Someone",
  needed_tracks: 31,
};

/** One an admin approved with a note: its tracks are queued. */
export const approvedAlbum: DownloadRequest = {
  ...waitingAlbum,
  id: "req-approved",
  status: "approved",
  queued_tracks: 31,
  decision_note: "Enjoy",
  decided_at: "2026-10-04T09:00:00+00:00",
};

/** One an admin denied, with the reason. */
export const deniedArtist: DownloadRequest = {
  ...waitingPlaylist,
  id: "req-denied",
  kind: "artist",
  reason: "kind_requires_approval",
  target: { provider: "catalog", id: "artist-1" },
  title: "Someone",
  needed_tracks: 120,
  status: "denied",
  decision_note: "Too much for this week",
  decided_at: "2026-10-04T09:30:00+00:00",
};

/** The likes download: no target, no cover. */
export const waitingLikes: DownloadRequest = {
  ...waitingPlaylist,
  id: "req-likes",
  kind: "liked_elsewhere",
  reason: "over_threshold",
  target: { provider: "", id: "" },
  title: "",
  needed_tracks: 120,
};

/** The admin's view of a request: who asked and who decided. */
export const asAdmin = (request: DownloadRequest, requester = "alice", decider: string | null = null): AdminDownloadRequest => ({
  ...request,
  requested_by: { id: `user-${requester}`, username: requester },
  decided_by: decider ? { id: `user-${decider}`, username: decider } : null,
});

/** The 202 answer of POST /download/playlist while the request waits. */
export const pendingApproval: PendingApprovalResponse = {
  status: "pending_approval",
  request_id: "req-playlist",
  kind: "playlist",
  needed_tracks: 259,
  reason: "kind_requires_approval",
};

/** What the progress stream sends the admins when a request is waiting (no `user_id`, with the count). */
export const eventPendingForAdmins = (request: AdminDownloadRequest, pending: number): DownloadRequestEvent => ({
  type: "download_request",
  event: "pending",
  request,
  pending,
});

/** What it sends the requester when their request was decided (with their `user_id`). */
export const eventDecidedForRequester = (request: DownloadRequest): DownloadRequestEvent => ({
  type: "download_request",
  event: request.status === "approved" ? "approved" : "denied",
  user_id: "user-alice",
  request,
});

export const decision = (id: string, outcome: DecisionResult["outcome"], request: AdminDownloadRequest | null): DecisionResult => ({
  id,
  outcome,
  request,
});

/** GET /admin/download-policy with the defaults of the backend. */
export const generalPolicy: DownloadPolicy = {
  rules: {
    track: { mode: "auto", threshold: 1 },
    album: { mode: "auto_up_to", threshold: 30 },
    playlist: { mode: "approval", threshold: 30 },
    artist: { mode: "approval", threshold: 30 },
    liked_elsewhere: { mode: "auto_up_to", threshold: 50 },
  },
  quota: { tracks: 200, window_days: 7 },
};

/** GET /admin/users/{id}/download-policy for a user nobody changed anything for. */
export const untouchedUserPolicy: UserDownloadPolicyView = {
  override: { rules: {}, quota_tracks: null },
  effective: generalPolicy,
};

/** The same user after PUT {"rules": {"playlist": {"mode": "auto_up_to", "threshold": 7}}, "quota_tracks": 9}. */
export const customizedUserPolicy: UserDownloadPolicyView = {
  override: { rules: { playlist: { mode: "auto_up_to", threshold: 7 } }, quota_tracks: 9 },
  effective: {
    rules: { ...generalPolicy.rules, playlist: { mode: "auto_up_to", threshold: 7 } },
    quota: { tracks: 9, window_days: 7 },
  },
};
