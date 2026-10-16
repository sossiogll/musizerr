import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { toast } from "sonner";
import { Check, Inbox, X } from "lucide-react";
import { adminListUsers, approveRequests, denyRequests, getAdminRequests } from "@/lib/api-client";
import { absoluteTime, relativeTime } from "@/lib/dates";
import { REQUEST_KINDS } from "@/lib/download-requests";
import { useFailureToast } from "@/lib/error-text";
import { queryKeys } from "@/lib/query-keys";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { EmptyState, PageNav } from "@/components/ContentList";
import { FilterTabs } from "@/components/FilterTabs";
import { ListToolbar, type FilterChip } from "@/components/ListToolbar";
import { NoteDialog } from "@/components/NoteDialog";
import { RequestStatusBadge, RequestSummary } from "@/components/requests/RequestSummary";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { usePendingRequestCount } from "@/hooks/usePendingRequestCount";
import { useRequestText } from "@/hooks/useRequestText";
import { useServerAction } from "@/hooks/useServerAction";
import { useI18n } from "@/i18n";
import type { AdminDownloadRequest, DecisionOutcome, DecisionResult, RequestKind } from "@/types";

const PAGE = 25;
const FILTERS = ["pending", "approved", "denied", "cancelled", "all"] as const;
type Filter = (typeof FILTERS)[number];
type Action = "approve" | "deny";

/** What the admin just decided: what each request came to (`results`), and what they were called then. */
interface Report {
  action: Action;
  results: DecisionResult[];
  names: ReadonlyMap<string, string>;
}

/**
 * Administration › Requests: the download requests the approval policy made
 * wait, and the history of the ones decided. Filter by state, kind and
 * requester; approve (with a note) or deny (with a reason) one request or a
 * selection of them, and read how each one came out. The progress stream
 * keeps the list and the badge current (RequestsWatcher): nothing is polled.
 */
export function AdminRequestsPage() {
  const { t } = useI18n();
  const text = useRequestText();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const failed = useFailureToast();
  const waiting = usePendingRequestCount();
  const [filter, setFilter] = useState<Filter>("pending");
  const [kind, setKind] = useState<RequestKind | "">("");
  const [userId, setUserId] = useState("");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [deciding, setDeciding] = useState<{ action: Action; ids: string[] } | null>(null);
  const [report, setReport] = useState<Report | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: queryKeys.adminRequests({ filter, kind, userId, page }),
    queryFn: () =>
      getAdminRequests({
        page,
        limit: PAGE,
        status: filter === "all" ? undefined : [filter],
        kind: kind ? [kind] : undefined,
        userId: userId || undefined,
      }),
    placeholderData: (previous) => previous,
  });
  const { data: users } = useQuery({ queryKey: queryKeys.adminUsers, queryFn: adminListUsers });
  const items = useMemo(() => data?.items ?? [], [data]);

  /** A new filter starts from the first page, with nothing selected. */
  const refilter = useCallback((change: () => void) => {
    change();
    setPage(1);
    setSelected(new Set());
  }, []);

  const decide = useMutation({
    mutationFn: ({ action, ids, note }: { action: Action; ids: string[]; note: string }) =>
      action === "approve" ? approveRequests(ids, note) : denyRequests(ids, note),
    onSuccess: (results, { action, ids }) => {
      const names = new Map(items.filter((r) => ids.includes(r.id)).map((r) => [r.id, text.name(r)]));
      setReport({ action, results, names });
      setSelected(new Set());
      const done = results.filter((r) => r.outcome === "decided").length;
      if (done === results.length) toast.success(t(action === "approve" ? "adminRequests.outcome.decided_approve" : "adminRequests.outcome.decided_deny", { count: done }));
      else toast.warning(t("adminRequests.decidedPartly", { done, total: results.length }));
    },
    onError: (error) => failed(error, t("adminRequests.decisionFailed")),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.adminRequests() });
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingRequestCount });
    },
  });

  const tabs = FILTERS.map((value) => ({
    value,
    label: value === "all" ? t("adminRequests.filterAll") : text.status(value),
    count: value === "pending" ? waiting : undefined,
  }));
  const chips: FilterChip[] = useMemo(
    () => [
      {
        key: "kind",
        label: t("adminRequests.filterKind"),
        options: REQUEST_KINDS.map((value) => ({ value, label: text.kind(value) })),
        value: kind,
        onChange: (value) => refilter(() => setKind(value as RequestKind | "")),
      },
      {
        key: "user",
        label: t("adminRequests.filterUser"),
        options: (users?.items ?? []).map((user) => ({ value: user.id, label: user.username })),
        value: userId,
        onChange: (value) => refilter(() => setUserId(value)),
      },
    ],
    [t, text, kind, userId, users, refilter],
  );

  const waitingHere = items.filter((r) => r.status === "pending");
  const allSelected = waitingHere.length > 0 && waitingHere.every((r) => selected.has(r.id));
  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  if (!data && error) return <ApiErrorMessage className="text-sm text-destructive" error={error} fallback={t("adminRequests.loadFailed")} />;

  return (
    <section aria-label={t("adminRequests.title")} className="space-y-4">
      <p className="max-w-prose text-sm text-muted-foreground">
        {t("adminRequests.intro")}{" "}
        <Link to="/config/$section" params={{ section: "library" }} className="font-medium text-primary underline underline-offset-4">
          {t("adminRequests.policyLink")}
        </Link>
      </p>

      <FilterTabs tabs={tabs} value={filter} onChange={(value) => refilter(() => setFilter(value))} label={t("adminRequests.filterStatus")} />
      <ListToolbar chips={chips} />

      {report && <DecisionReport report={report} onDismiss={() => setReport(null)} />}

      {selected.size > 0 && (
        <div className="sticky top-[var(--shell-top)] z-30 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-background/90 px-4 py-2 backdrop-blur">
          <span className="text-sm text-muted-foreground">{t("adminRequests.selected", { count: selected.size })}</span>
          <div className="flex flex-wrap gap-2">
            <Button variant="ghost" onClick={() => setSelected(new Set())}>
              {t("adminRequests.clearSelection")}
            </Button>
            <Button variant="outline" disabled={server.disabled || decide.isPending} title={server.reason} onClick={() => setDeciding({ action: "deny", ids: [...selected] })}>
              <X aria-hidden />
              {t("adminRequests.denySelected", { count: selected.size })}
            </Button>
            <Button disabled={server.disabled || decide.isPending} title={server.reason} onClick={() => setDeciding({ action: "approve", ids: [...selected] })}>
              <Check aria-hidden />
              {t("adminRequests.approveSelected", { count: selected.size })}
            </Button>
          </div>
        </div>
      )}

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-20" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <EmptyState icon={Inbox} message={filter === "pending" && !kind && !userId ? t("adminRequests.emptyPending") : t("adminRequests.empty")} />
      ) : (
        <>
          {waitingHere.length > 1 && (
            <label className="flex h-11 w-fit cursor-pointer items-center gap-3 text-sm">
              <input
                type="checkbox"
                className="size-5 accent-primary"
                checked={allSelected}
                onChange={() => setSelected(allSelected ? new Set() : new Set(waitingHere.map((r) => r.id)))}
              />
              {t("adminRequests.selectAll", { count: waitingHere.length })}
            </label>
          )}
          <ul className="space-y-2">
            {items.map((request) => (
              <li key={request.id}>
                <RequestRow
                  request={request}
                  selected={selected.has(request.id)}
                  onToggle={() => toggle(request.id)}
                  onDecide={(action) => setDeciding({ action, ids: [request.id] })}
                  disabled={server.disabled || decide.isPending}
                  disabledReason={server.reason}
                />
              </li>
            ))}
          </ul>
        </>
      )}
      <PageNav
        page={page}
        pages={data?.pages ?? 0}
        onPageChange={(next) => {
          setPage(next);
          setSelected(new Set());
        }}
      />

      <NoteDialog
        open={deciding !== null}
        onOpenChange={(open) => !open && setDeciding(null)}
        title={t(deciding?.action === "deny" ? "adminRequests.denyTitle" : "adminRequests.approveTitle", { count: deciding?.ids.length ?? 0 })}
        description={t(deciding?.action === "deny" ? "adminRequests.denyBody" : "adminRequests.approveBody")}
        label={t(deciding?.action === "deny" ? "adminRequests.denyNote" : "adminRequests.approveNote")}
        confirmLabel={t(deciding?.action === "deny" ? "adminRequests.deny" : "adminRequests.approve")}
        destructive={deciding?.action === "deny"}
        onConfirm={(note) => deciding && decide.mutate({ ...deciding, note })}
      />
    </section>
  );
}

/** One request: what it is, who asked and when, how it was decided, and the choice to make while it waits. */
function RequestRow({
  request,
  selected,
  onToggle,
  onDecide,
  disabled,
  disabledReason,
}: {
  request: AdminDownloadRequest;
  selected: boolean;
  onToggle: () => void;
  onDecide: (action: Action) => void;
  disabled: boolean;
  disabledReason: string | undefined;
}) {
  const { t, locale } = useI18n();
  const text = useRequestText();
  const pending = request.status === "pending";
  const name = text.name(request);
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-3 rounded-lg border p-3">
      {pending && (
        <label className="grid size-11 shrink-0 cursor-pointer place-items-center">
          <input type="checkbox" className="size-5 accent-primary" checked={selected} onChange={onToggle} aria-label={t("adminRequests.select", { title: name })} />
        </label>
      )}
      <RequestSummary request={request} className="min-w-0 flex-1 basis-64">
        <p className="text-xs text-muted-foreground">
          {t("adminRequests.askedBy", { name: request.requested_by.username ?? "—" })}
          {" · "}
          <time dateTime={request.created_at} title={absoluteTime(request.created_at, locale)}>
            {relativeTime(request.created_at, locale)}
          </time>
        </p>
        {request.decided_at && request.decided_by && (
          <p className="text-xs text-muted-foreground">
            {t("adminRequests.decidedBy", { name: request.decided_by.username ?? "—" })}
            {" · "}
            <time dateTime={request.decided_at} title={absoluteTime(request.decided_at, locale)}>
              {relativeTime(request.decided_at, locale)}
            </time>
          </p>
        )}
        {request.decision_note && <p className="text-sm text-muted-foreground">{t("adminRequests.note", { note: request.decision_note })}</p>}
      </RequestSummary>
      <div className="flex items-center gap-2">
        <RequestStatusBadge status={request.status} />
        {pending && (
          <>
            <Button variant="outline" disabled={disabled} title={disabledReason} aria-label={t("adminRequests.denyItem", { title: name })} onClick={() => onDecide("deny")}>
              <X aria-hidden />
              {t("adminRequests.deny")}
            </Button>
            <Button disabled={disabled} title={disabledReason} aria-label={t("adminRequests.approveItem", { title: name })} onClick={() => onDecide("approve")}>
              <Check aria-hidden />
              {t("adminRequests.approve")}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

/** How many of a decision's outcomes there are. */
function tally(results: DecisionResult[]): Partial<Record<DecisionOutcome, number>> {
  const counts: Partial<Record<DecisionOutcome, number>> = {};
  for (const { outcome } of results) counts[outcome] = (counts[outcome] ?? 0) + 1;
  return counts;
}

/** The key of an outcome's words, which for "decided" and "unchanged" depend on what was asked (approving or denying). */
const outcomeKey = (action: Action, outcome: DecisionOutcome, group: "outcome" | "detail"): string =>
  `adminRequests.${group}.${outcome === "decided" || outcome === "unchanged" ? `${outcome}_${action}` : outcome}`;

const OUTCOMES: readonly DecisionOutcome[] = ["decided", "unchanged", "conflict", "not_found", "failed"];

/**
 * What a decision did, request by request: a line that counts each kind of
 * outcome (decided, already so, decided otherwise meanwhile, gone, could not
 * be queued) and, under it, each request that did not simply go through.
 */
function DecisionReport({ report, onDismiss }: { report: Report; onDismiss: () => void }) {
  const { t, tx } = useI18n();
  const text = useRequestText();
  const counts = tally(report.results);
  const problems = report.results.filter((r) => r.outcome !== "decided");
  return (
    <div role="status" className="space-y-2 rounded-lg border bg-card p-3">
      <div className="flex items-start justify-between gap-2">
        <p className="pt-2.5 text-sm font-medium">
          {OUTCOMES.filter((outcome) => counts[outcome])
            .map((outcome) => tx(outcomeKey(report.action, outcome, "outcome"), outcome, { count: counts[outcome] ?? 0 }))
            .join(" · ")}
        </p>
        <Button variant="ghost" size="icon" className="shrink-0" aria-label={t("adminRequests.dismissReport")} onClick={onDismiss}>
          <X aria-hidden />
        </Button>
      </div>
      {problems.length > 0 && (
        <ul className="space-y-1 text-sm">
          {problems.map(({ id, outcome, request }) => (
            <li key={id} className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-medium">{report.names.get(id) ?? (request ? text.name(request) : id)}</span>
              <span className="text-muted-foreground">
                {tx(outcomeKey(report.action, outcome, "detail"), outcome, { status: request ? text.status(request.status) : "" })}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
