import { useCallback, useEffect, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useProgressEvents } from "@/contexts/QueueContext";
import { useRequestText } from "@/hooks/useRequestText";
import { useI18n } from "@/i18n";
import { queryKeys } from "@/lib/query-keys";
import type { DownloadRequestEvent, StreamEvent } from "@/types";

/** A burst of events (a bulk decision) refreshes the admin's list once, not once per request. */
const REFRESH_AFTER_MS = 250;

/**
 * Mounted once by the shell: what the progress stream says about download
 * requests, from any page.
 *
 * - To an admin (an event with no `user_id`): the number of requests waiting
 *   is written as the stream sends it (the badge needs no request of its
 *   own) and the admin's list is read again.
 * - To a requester (with `user_id`): their own lists are read again, and a
 *   toast says how it ended: approved (the tracks are on their way) or denied
 *   (with the admin's reason, if any).
 */
export function RequestsWatcher() {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const text = useRequestText();
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => () => clearTimeout(timer.current), []);

  /** How the requester's request ended. */
  const announce = useCallback(
    ({ event, request }: DownloadRequestEvent) => {
      const name = text.name(request);
      if (event === "approved") {
        toast.success(t("approval.approvedToast", { title: name }), {
          description: request.decision_note ? t("approval.approvedNote", { note: request.decision_note }) : undefined,
        });
        void queryClient.invalidateQueries({ queryKey: queryKeys.downloads() });
        void queryClient.invalidateQueries({ queryKey: queryKeys.downloadsSummary });
      } else if (event === "denied") {
        toast.warning(t("approval.deniedToast", { title: name }), {
          description: request.decision_note ? t("approval.deniedReason", { reason: request.decision_note }) : undefined,
        });
      }
    },
    [queryClient, t, text],
  );

  const onEvent = useCallback(
    (event: StreamEvent) => {
      if (event.type !== "download_request") return;
      if (event.user_id === undefined) {
        if (event.pending !== undefined) queryClient.setQueryData(queryKeys.pendingRequestCount, { pending: event.pending });
        clearTimeout(timer.current);
        timer.current = setTimeout(() => void queryClient.invalidateQueries({ queryKey: queryKeys.adminRequests() }), REFRESH_AFTER_MS);
        return;
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.myRequests() });
      announce(event);
    },
    [queryClient, announce],
  );

  useProgressEvents(onEvent);
  return null;
}
