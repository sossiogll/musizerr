import type { LibraryCoverage, PendingApprovalResponse, RequestKind, RequestMode, RequestStatus, ViewerDownloadPolicy } from "@/types";

/** The kinds of request and the states of one, in the order the UI lists them. */
export const REQUEST_KINDS: readonly RequestKind[] = ["track", "album", "playlist", "artist", "liked_elsewhere"];
export const REQUEST_STATUSES: readonly RequestStatus[] = ["pending", "approved", "denied", "cancelled"];
/** How a request may be asked to be decided: the modes of the policy. */
export const REQUEST_MODES: readonly RequestMode[] = ["auto", "approval", "auto_up_to"];

/** Whether a download route answered that the request waits for an admin (the approval policy), not that it ran. */
export const isPendingApproval = (res: object): res is PendingApprovalResponse =>
  (res as { status?: unknown }).status === "pending_approval";

/**
 * What a request is found by: the kind and what it asks for. A request that
 * asks for no particular target (`liked_elsewhere`) has an empty provider and id.
 */
export const targetKey = (kind: RequestKind, provider = "", id = ""): string => `${kind}:${provider}:${id}`;

/**
 * The tracks of a collection that are neither in the library nor on their
 * way, as the server counted them (`library`), or all of `declared` when
 * nothing is known of the library; null when no source says how many tracks
 * the collection has.
 */
export function missingTracks(library: LibraryCoverage | undefined, declared: number | null | undefined): number | null {
  const total = library?.total ?? declared ?? null;
  if (total === null) return null;
  return Math.max(0, total - (library?.downloaded ?? 0) - (library?.in_progress ?? 0));
}

/** What the viewer's policy says about a request of one kind. */
export interface ApprovalVerdict {
  /** The request would wait for an admin. */
  waits: boolean;
  /**
   * The most tracks that need a download a request of this kind can have and
   * still go through at once right now (null = no limit, 0 = it always waits).
   */
  limit: number | null;
}

const NOT_GOVERNED: ApprovalVerdict = { waits: false, limit: null };

/**
 * Reads, never decides: the server computed `auto_up_to` for this viewer and
 * kind (quota included); this only holds a request of `needed` tracks
 * against it, to label a button honestly before it is pressed. `needed` is
 * null when the page does not know how many tracks the request would need.
 * The server judges the request itself when it is made.
 */
export function approvalVerdict(policy: ViewerDownloadPolicy | null | undefined, kind: RequestKind, needed: number | null): ApprovalVerdict {
  const rule = policy?.applies ? policy.rules?.[kind] : undefined;
  if (!rule) return NOT_GOVERNED;
  const limit = rule.auto_up_to;
  if (limit === null) return NOT_GOVERNED;
  const waits = limit === 0 ? needed !== 0 : needed !== null && needed > limit;
  return { waits, limit };
}
