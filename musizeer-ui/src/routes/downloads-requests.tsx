import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Hourglass, Loader2, X } from "lucide-react";
import { cancelMyRequest, getMyRequests } from "@/lib/api-client";
import { absoluteTime, relativeTime } from "@/lib/dates";
import { REQUEST_STATUSES } from "@/lib/download-requests";
import { useFailureToast } from "@/lib/error-text";
import { queryKeys } from "@/lib/query-keys";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { EmptyState, PageNav } from "@/components/ContentList";
import { ListToolbar, type FilterChip } from "@/components/ListToolbar";
import { RequestStatusBadge, RequestSummary } from "@/components/requests/RequestSummary";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useRequestText } from "@/hooks/useRequestText";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { DownloadRequest, RequestStatus } from "@/types";

const PAGE = 25;

/**
 * Downloads › Requests: what the viewer asked for under the approval policy
 * and where each request stands — waiting for an admin, approved, denied
 * (with the reason) or cancelled — newest first, page by page. A waiting one
 * can be taken back. The progress stream keeps it current (RequestsWatcher).
 */
export function MyRequestsPanel() {
  const { t } = useI18n();
  const text = useRequestText();
  const [status, setStatus] = useState<RequestStatus | "">("");
  const [page, setPage] = useState(1);
  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.myRequests({ status, page }),
    queryFn: () => getMyRequests({ page, limit: PAGE, status: status ? [status] : undefined }),
    placeholderData: (previous) => previous,
  });

  const chips: FilterChip[] = useMemo(
    () => [
      {
        key: "status",
        label: t("requests.filterStatus"),
        options: REQUEST_STATUSES.map((value) => ({ value, label: text.status(value) })),
        value: status,
        onChange: (value) => {
          setStatus(value as RequestStatus | "");
          setPage(1);
        },
      },
    ],
    [t, text, status],
  );

  if (!data && error) {
    return <ApiErrorMessage className="py-8 text-center text-sm text-muted-foreground" error={error} fallback={t("errors.listFailed")} />;
  }

  return (
    <section aria-label={t("requests.title")} className="space-y-3">
      <p className="max-w-prose text-sm text-muted-foreground">{t("requests.intro")}</p>
      <ListToolbar chips={chips} />
      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-20" />
          ))}
        </div>
      ) : data?.items.length ? (
        <ul className="space-y-2">
          {data.items.map((request) => (
            <li key={request.id}>
              <RequestRow request={request} />
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState icon={Hourglass} message={status ? t("requests.noMatch") : t("requests.empty")} />
      )}
      <PageNav page={page} pages={data?.pages ?? 0} onPageChange={setPage} />
    </section>
  );
}

/** One request: what it is, where it stands, when, and — while it waits — the way to take it back. */
function RequestRow({ request }: { request: DownloadRequest }) {
  const { t, locale } = useI18n();
  const text = useRequestText();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const failed = useFailureToast();
  const name = text.name(request);
  const cancel = useMutation({
    mutationFn: () => cancelMyRequest(request.id),
    onSuccess: ({ changed }) => {
      if (changed) toast.success(t("requests.cancelled", { title: name }));
    },
    // An admin may have decided it meanwhile (409): the list says how it stands now.
    onError: (error) => failed(error, t("requests.cancelFailed")),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.myRequests() }),
  });

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-3 rounded-lg border p-3">
      <RequestSummary request={request} className="min-w-0 flex-1 basis-64">
        <p className="text-xs text-muted-foreground">
          <time dateTime={request.created_at} title={absoluteTime(request.created_at, locale)}>
            {t("requests.asked", { when: relativeTime(request.created_at, locale) })}
          </time>
          {request.decided_at && (
            <>
              {" · "}
              <time dateTime={request.decided_at} title={absoluteTime(request.decided_at, locale)}>
                {t("requests.decided", { when: relativeTime(request.decided_at, locale) })}
              </time>
            </>
          )}
        </p>
        {request.status === "denied" && request.decision_note && (
          <p className="text-sm text-destructive">{t("approval.deniedReason", { reason: request.decision_note })}</p>
        )}
        {request.status === "approved" && request.decision_note && (
          <p className="text-sm text-muted-foreground">{t("approval.approvedNote", { note: request.decision_note })}</p>
        )}
      </RequestSummary>
      <div className="flex items-center gap-2">
        <RequestStatusBadge status={request.status} />
        {request.status === "pending" && (
          <Button
            variant="outline"
            onClick={() => cancel.mutate()}
            disabled={cancel.isPending || server.disabled}
            title={server.reason}
            aria-label={t("requests.cancelItem", { title: name })}
          >
            {cancel.isPending ? <Loader2 aria-hidden className="animate-spin" /> : <X aria-hidden />}
            {t("common.cancel")}
          </Button>
        )}
      </div>
    </div>
  );
}
