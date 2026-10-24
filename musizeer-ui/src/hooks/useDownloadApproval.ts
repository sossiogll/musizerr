import { useCallback, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAuth } from "@/contexts/AuthContext";
import { useRequestText } from "@/hooks/useRequestText";
import { getMyRequests, type RequestsQuery } from "@/lib/api-client";
import { approvalVerdict, targetKey } from "@/lib/download-requests";
import { queryKeys } from "@/lib/query-keys";
import { useI18n } from "@/i18n";
import type { DownloadRequest, PaginatedResponse, PendingApprovalResponse, RequestKind } from "@/types";

/** What the button of a download tells before it is pressed. */
export interface Approval {
  /** The request would wait for an admin: the button says "Request". */
  waits: boolean;
  /** A short sentence for the tooltip ("Needs approval: 259 tracks"), if there is something to say. */
  hint: string | undefined;
}

/**
 * Whether a download of `kind` would wait for an admin for this viewer, from
 * the policy the server reported (`download_policy` of the viewer's status):
 * an admin, and open mode, never wait. `needed` is how many tracks the
 * request would need, null when the page does not know. The server decides
 * the request itself when it is made.
 */
export function useApproval(kind: RequestKind, needed: number | null): Approval {
  const { authConfig } = useAuth();
  const { t } = useI18n();
  const { waits, limit } = approvalVerdict(authConfig?.download_policy, kind, needed);
  let hint: string | undefined;
  if (waits) {
    if (needed === null) hint = t("approval.needsApproval");
    else hint = limit ? t("approval.needsApprovalLimit", { count: needed, limit }) : t("approval.needsApprovalTracks", { count: needed });
  } else if (limit !== null && needed === null) {
    // Goes through, but only up to a size the page cannot compare with.
    hint = t("approval.upTo", { count: limit });
  }
  // Stable: the memoized rows that receive it do not re-render for nothing.
  return useMemo(() => ({ waits, hint }), [waits, hint]);
}

/** The viewer's own requests that wait: one list for every button and row of the app. */
const WAITING: RequestsQuery = { status: ["pending"], limit: 200 };

/**
 * One read of the waiting list shared by everything that needs a piece of
 * it, each taking its own piece (`select`). Nothing is asked of the server
 * for a viewer the policy does not govern (admins, open mode, guests).
 */
function useWaiting<T>(select: (page: PaginatedResponse<DownloadRequest>) => T): T | undefined {
  const { authConfig } = useAuth();
  return useQuery({
    queryKey: queryKeys.myRequests(WAITING),
    queryFn: () => getMyRequests(WAITING),
    enabled: authConfig?.download_policy?.applies === true,
    staleTime: 30_000,
    select,
  }).data;
}

/**
 * The viewer's request for this target that still waits for an admin, if
 * any: what a download button shows as "Waiting for approval" instead of
 * offering the download again.
 */
export function useWaitingRequest(kind: RequestKind, provider?: string, id?: string): DownloadRequest | undefined {
  const key = targetKey(kind, provider, id);
  const select = useCallback(
    (page: PaginatedResponse<DownloadRequest>) => page.items.find((r) => targetKey(r.kind, r.target.provider, r.target.id) === key),
    [key],
  );
  return useWaiting(select);
}

const total = (page: PaginatedResponse<DownloadRequest>) => page.total;

/** How many of the viewer's requests wait for an admin (undefined until known): the counter of the Requests tab. */
export function useWaitingCount(): number | undefined {
  return useWaiting(total);
}

/**
 * What every download does with a 202 `pending_approval` (or a likes
 * download that ends waiting, `LibraryJobResult.pending_approval`): nothing was queued
 * and no favorite set, so it says the request was sent (and why it waits)
 * instead of "queued", and the waiting lists are read again so the item shows
 * its state. The ONE place that words it.
 */
export function useAnnouncePending() {
  const { t } = useI18n();
  const text = useRequestText();
  const queryClient = useQueryClient();
  return useCallback(
    (res: Pick<PendingApprovalResponse, "needed_tracks" | "reason">) => {
      toast.info(t("approval.sent", { count: res.needed_tracks }), { description: text.reason(res.reason) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.myRequests() });
    },
    [t, text, queryClient],
  );
}
