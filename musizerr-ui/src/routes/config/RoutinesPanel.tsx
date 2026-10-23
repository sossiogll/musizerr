import { useEffect, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  getRoutines,
  patchRoutine,
  runRoutineNow,
  type RoutineInfo,
  type RoutineProviderReport,
  type RoutineRunProgress,
} from "@/lib/api-client";
import { Card } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { ErrorMessage } from "@/components/ErrorMessage";
import { ProgressBar } from "@/components/ProgressBar";
import { Activity, AlertTriangle, Clock, Loader2 } from "lucide-react";
import { absoluteTime, formatDuration, parseApiDate, relativeTime } from "@/lib/dates";
import { useErrorText } from "@/lib/error-text";
import { useCan } from "@/hooks/useCapabilities";
import { usePublicProviders } from "@/hooks/usePublicProviders";
import { useI18n, type MessageKey } from "@/i18n";

const POLL_MS = 15_000;

/** Localized label for a routine key, by convention `routines.names.<key>` —
 * an unknown key (a routine declared backend-side that this build's i18n
 * dictionary hasn't caught up with yet) falls back to the raw key instead of
 * the dotted message path or a crash. */
function routineName(t: (key: MessageKey, vars?: Record<string, string | number>) => string, key: string): string {
  const messageKey = `routines.names.${key}`;
  const resolved = t(messageKey as MessageKey);
  return resolved === messageKey ? key : resolved;
}

function routineDescription(
  t: (key: MessageKey, vars?: Record<string, string | number>) => string,
  key: string,
): string {
  const messageKey = `routines.descriptions.${key}`;
  const resolved = t(messageKey as MessageKey);
  return resolved === messageKey ? "" : resolved;
}

/** Compact human-readable cadence, e.g. "6 h" for 360 minutes or "45 min"
 * otherwise — kept simple on purpose (see CLAUDE.md task brief), never a
 * full duration-formatting library for a single hint line. */
function intervalHintValue(minutes: number): string {
  if (minutes >= 60 && minutes % 60 === 0) return `${minutes / 60} h`;
  return `${minutes} min`;
}

type Translate = (key: string, fallback: string, vars?: Record<string, string | number>) => string;

/**
 * A successful run's result in words: the routine's own sentence when the
 * result carries every number it needs, otherwise its numbers labelled one
 * by one (a routine this build does not know yet, or a result that said
 * why it did nothing).
 */
export function routineResultText(tx: Translate, key: string, result: Record<string, unknown>): string | null {
  return sentence(tx, `routines.results.${key}`, result);
}

/** One provider's report of a run (`result.providers[i]`) in words: the
 * routine's per-provider sentence, or its numbers labelled one by one. */
export function providerReportText(tx: Translate, key: string, report: RoutineProviderReport): string | null {
  return sentence(tx, `routines.providerResults.${key}`, report);
}

function sentence(tx: Translate, templateKey: string, result: Record<string, unknown>): string | null {
  const numbers: Record<string, number> = {};
  for (const [name, value] of Object.entries(result)) if (typeof value === "number") numbers[name] = value;
  const template = tx(templateKey, "");
  const needed = Array.from(template.matchAll(/\{\{(\w+)\}\}/g), (m) => m[1]);
  if (template && needed.every((name) => name in numbers)) return tx(templateKey, "", numbers);
  const parts = Object.entries(numbers).map(([name, value]) => `${tx(`routines.resultFields.${name}`, name)} ${value}`);
  if (typeof result.detail === "string") parts.push(result.detail);
  return parts.length ? parts.join(" · ") : null;
}

/** The per-provider reports of a run's result, when it has them. */
function providerReports(result: Record<string, unknown> | null | undefined): RoutineProviderReport[] | null {
  const providers = result?.providers;
  return Array.isArray(providers) ? (providers as RoutineProviderReport[]) : null;
}

/** Where a running run is, in words, with the provider's label. */
export function progressText(tx: Translate, label: (id: string) => string, progress: RoutineRunProgress): string {
  const vars = {
    provider: progress.provider ? label(progress.provider) : "",
    processed: progress.processed,
    total: progress.total ?? 0,
  };
  const generic = progress.total != null ? tx("routines.phaseCount", "", vars) : "";
  return tx(`routines.phases.${progress.phase}`, generic || progress.phase, vars);
}

/** What the like matching's result says about the library it matched against. */
interface LibraryIndexState {
  status: string;
  error_code?: string | null;
  error_message?: string | null;
}

function libraryIndexState(result: Record<string, unknown> | null | undefined): LibraryIndexState | null {
  const state = result?.library_index;
  return state && typeof state === "object" && "status" in state ? (state as LibraryIndexState) : null;
}

function errorDetail(err: unknown): string | undefined {
  return (err as { response?: { data?: { detail?: string } } } | null)?.response?.data?.detail;
}

/**
 * Admin-only dedicated section for the "system routines" (periodic
 * background jobs, see app/workers/routines.py). Their enabled/interval
 * settings also exist in the generic settings schema (group "routines"),
 * but declared `hidden: true` there — this is the page that actually owns
 * them (see SettingsArea, which skips anything `hidden`).
 *
 * Self-gated on the `admin` capability (true for everyone with sign-in
 * off) so it can be dropped anywhere unconditionally.
 */
export function RoutinesPanel() {
  const isAdmin = useCan("admin");
  const { t, tx, locale } = useI18n();
  const queryClient = useQueryClient();

  const { data: routines, isLoading, isError } = useQuery({
    queryKey: ["system-routines"],
    queryFn: getRoutines,
    // Live view: a routine started elsewhere (or by the "Run now" button
    // below) should flip from running -> finished without a manual refresh.
    refetchInterval: POLL_MS,
    enabled: isAdmin,
  });

  if (!isAdmin) return null;

  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">{t("routines.title")}</h3>
      <p className="mb-3 text-xs text-muted-foreground">{t("routines.description")}</p>
      <Separator className="mb-3" />

      {isLoading && (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-20 rounded-lg bg-muted animate-pulse" />
          ))}
        </div>
      )}

      {isError && <p className="text-xs text-destructive">{t("routines.loadFailed")}</p>}

      {routines?.some((r) => r.scheduler_stale) && (
        <div role="alert" className="mb-3 flex items-start gap-2 rounded-md border border-warning/30 bg-warning/10 p-3 text-sm">
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-warning" />
          <div>
            <p className="font-medium">{t("routines.schedulerStaleTitle")}</p>
            <p className="text-muted-foreground">{t("routines.schedulerStaleBody")}</p>
          </div>
        </div>
      )}

      {routines && (
        <div className="space-y-3">
          {routines.map((r) => (
            <RoutineRow
              key={r.key}
              routine={r}
              t={t}
              tx={tx}
              locale={locale}
              onChanged={() => queryClient.invalidateQueries({ queryKey: ["system-routines"] })}
            />
          ))}
        </div>
      )}
    </Card>
  );
}

function statusBadgeVariant(status: string): "success" | "destructive" | "info" | "secondary" {
  if (status === "ok") return "success";
  if (status === "error") return "destructive";
  if (status === "running") return "info";
  return "secondary";
}

const STATUS_LABELS: Record<string, MessageKey> = {
  ok: "routines.statusOk",
  error: "routines.statusError",
  running: "routines.statusRunning",
  queued: "routines.statusQueued",
  skipped: "routines.statusSkipped",
};

/** Seconds since an API time, never negative (a clock a little ahead). */
function secondsSince(iso: string): number {
  return Math.max(0, (Date.now() - parseApiDate(iso).getTime()) / 1000);
}

interface RoutineRowProps {
  routine: RoutineInfo;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
  tx: Translate;
  locale: string;
  onChanged: () => void;
}

function RoutineRow({ routine, t, tx, locale, onChanged }: RoutineRowProps) {
  const [intervalInput, setIntervalInput] = useState(String(routine.interval_minutes));
  const [detailExpanded, setDetailExpanded] = useState(false);
  const dirtyRef = useRef(false);

  // Resync from the server value while the user isn't mid-edit — keeps the
  // ~15s poll from clobbering an in-progress edit, but still reflects a
  // value changed elsewhere (or the server's own normalization) otherwise.
  useEffect(() => {
    if (!dirtyRef.current) setIntervalInput(String(routine.interval_minutes));
  }, [routine.interval_minutes]);

  const patchMutation = useMutation({
    mutationFn: (updates: { enabled?: boolean; interval_minutes?: number }) => patchRoutine(routine.key, updates),
    onSuccess: () => {
      dirtyRef.current = false;
      onChanged();
    },
  });

  const runMutation = useMutation({
    mutationFn: () => runRoutineNow(routine.key),
    onSuccess: () => {
      toast.success(t("routines.runStarted"));
      onChanged();
    },
    onError: (err: unknown) => {
      const status = (err as { response?: { status?: number } } | null)?.response?.status;
      if (status === 409) {
        toast.error(t("routines.runAlreadyRunning"));
      } else {
        toast.error(errorDetail(err) ?? t("routines.runFailed"));
      }
    },
  });

  const handleToggle = (checked: boolean) => {
    patchMutation.mutate(
      { enabled: checked },
      { onError: (err) => toast.error(errorDetail(err) ?? t("routines.enabledSaveFailed")) },
    );
  };

  const commitInterval = () => {
    const parsed = Number(intervalInput);
    if (!Number.isFinite(parsed) || !Number.isInteger(parsed) || parsed < routine.min_interval_minutes) {
      toast.error(t("routines.intervalBelowMinimum", { min: routine.min_interval_minutes }));
      dirtyRef.current = false;
      setIntervalInput(String(routine.interval_minutes));
      return;
    }
    if (parsed === routine.interval_minutes) {
      dirtyRef.current = false;
      return;
    }
    patchMutation.mutate(
      { interval_minutes: parsed },
      {
        onSuccess: () => toast.success(t("routines.saved")),
        onError: (err) => {
          toast.error(errorDetail(err) ?? t("routines.intervalSaveFailed"));
          dirtyRef.current = false;
          setIntervalInput(String(routine.interval_minutes));
        },
      },
    );
  };

  const name = routineName(t, routine.key);
  const description = routineDescription(t, routine.key);
  const lastRun = routine.last_run;
  const failed = lastRun?.status === "error";
  // Waiting in the queue (since the dispatch) or executing (since the real
  // start): never "running" for a run no worker has picked up yet.
  const running = routine.running && lastRun?.status === "running" && !!lastRun.started_at;
  const queued = !!routine.queued && lastRun?.status === "queued";
  const active = running || queued;
  // When the last run happened: its real start, the dispatch for a run that
  // never started (a skipped one), or the old single time of a legacy row.
  const lastRunAt = lastRun ? (lastRun.started_at ?? lastRun.queued_at) : null;
  // A failed run is shown through its error code; the raw detail only in
  // the collapsible block the server fills for admins. A result is data,
  // put in words per routine — per provider when it has a report for each
  // (a provider that failed shows its own error there).
  const reports = active ? null : providerReports(lastRun?.result);
  const indexState = active ? null : libraryIndexState(lastRun?.result);
  const detail =
    failed || active || reports
      ? null
      : lastRun?.result
        ? routineResultText(tx, routine.key, lastRun.result)
        : (lastRun?.detail ?? null);
  const progress = running ? (lastRun?.progress ?? null) : null;
  const activeWork = routine.active_work
    ? tx(`routines.activeWork.${routine.key}`, t("routines.activeWorkGeneric"), { count: routine.active_work })
    : null;
  const detailIsLong = !!detail && detail.length > 120;
  const shownDetail = detail && detailIsLong && !detailExpanded ? `${detail.slice(0, 120)}…` : detail;

  return (
    <div className="rounded-lg border p-3">
      <div className="flex flex-col gap-1.5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 sm:pr-4">
          <div className="text-sm font-medium">{name}</div>
          {description && <div className="text-xs text-muted-foreground">{description}</div>}
          {routine.follows && (
            <div className="text-xs text-muted-foreground">
              {t("routines.followsHint", { name: routineName(t, routine.follows) })}
            </div>
          )}
        </div>
        <Switch
          checked={routine.enabled}
          onCheckedChange={handleToggle}
          aria-label={name}
          disabled={patchMutation.isPending}
          className="shrink-0"
        />
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <div className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">{t("routines.intervalLabel")}</div>
          <div className="flex items-center gap-2">
            <Input
              type="number"
              className="w-20"
              aria-label={t("routines.intervalLabel")}
              min={routine.min_interval_minutes}
              value={intervalInput}
              onChange={(e) => {
                dirtyRef.current = true;
                setIntervalInput(e.target.value);
              }}
              onBlur={commitInterval}
              onKeyDown={(e) => {
                if (e.key === "Enter") e.currentTarget.blur();
              }}
            />
            <span className="text-xs text-muted-foreground">{t("routines.intervalUnitMinutes")}</span>
          </div>
          <div className="text-xs text-muted-foreground">
            {t("routines.intervalHint", { value: intervalHintValue(routine.interval_minutes) })}
          </div>
        </div>

        <div className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">{t("routines.lastRun")}</div>
          {lastRun ? (
            <>
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                {running && lastRun.started_at ? (
                  <>
                    <Badge variant="info" className="gap-1">
                      <Loader2 aria-hidden className="size-3 animate-spin" />
                      {t("routines.statusRunning")}
                    </Badge>
                    <span className="text-xs" title={absoluteTime(lastRun.started_at, locale)}>
                      {t("routines.runningSince", { elapsed: formatDuration(secondsSince(lastRun.started_at), locale) })}
                    </span>
                  </>
                ) : queued ? (
                  <>
                    <Badge variant="secondary" className="gap-1">
                      <Clock aria-hidden className="size-3" />
                      {t("routines.statusQueued")}
                    </Badge>
                    <span className="text-xs" title={absoluteTime(lastRun.queued_at, locale)}>
                      {t("routines.queuedSince", { elapsed: formatDuration(secondsSince(lastRun.queued_at), locale) })}
                    </span>
                  </>
                ) : (
                  <>
                    <Badge variant={statusBadgeVariant(lastRun.status)}>
                      {t(STATUS_LABELS[lastRun.status] ?? "routines.statusRunning")}
                    </Badge>
                    {lastRunAt && (
                      <span className="text-xs" title={absoluteTime(lastRunAt, locale)}>
                        {relativeTime(lastRunAt, locale)}
                      </span>
                    )}
                    {lastRun.duration_seconds != null && (
                      <span className="text-xs text-muted-foreground">
                        {t("routines.tookDuration", { duration: formatDuration(lastRun.duration_seconds, locale) })}
                      </span>
                    )}
                    {lastRun.wait_seconds != null && lastRun.wait_seconds >= 1 && (
                      <span className="text-xs text-muted-foreground">
                        {t("routines.waitedDuration", { duration: formatDuration(lastRun.wait_seconds, locale) })}
                      </span>
                    )}
                  </>
                )}
              </div>
              {progress && <RunProgressLine progress={progress} tx={tx} />}
              {failed && lastRun && !reports && (
                <ErrorMessage
                  className="text-xs text-destructive"
                  code={lastRun.error_code}
                  message={lastRun.error_message}
                  detail={lastRun.error_detail}
                />
              )}
              {reports && <ProviderReports routineKey={routine.key} reports={reports} tx={tx} />}
              {indexState && <LibraryIndexNote state={indexState} t={t} />}
              {shownDetail && (
                <div className="text-xs text-muted-foreground break-words">
                  {shownDetail}
                  {detailIsLong && (
                    <button
                      type="button"
                      className="ml-1 text-primary hover:underline"
                      onClick={() => setDetailExpanded((v) => !v)}
                    >
                      {t(detailExpanded ? "routines.hideDetail" : "routines.showDetail")}
                    </button>
                  )}
                </div>
              )}
            </>
          ) : (
            <span className="text-xs text-muted-foreground">{t("routines.neverRun")}</span>
          )}
          {activeWork && (
            <div className="flex items-center gap-1.5 text-xs text-info">
              <Activity aria-hidden className="size-3.5 shrink-0" />
              <span>{activeWork}</span>
            </div>
          )}
        </div>

        <div className="space-y-2">
          <div>
            <div className="text-xs font-medium text-muted-foreground">{t("routines.nextRun")}</div>
            {routine.scheduler_stale ? (
              <span className="text-xs font-medium text-warning" title={routine.next_run_at ? absoluteTime(routine.next_run_at, locale) : undefined}>
                {t("routines.schedulerStaleShort")}
              </span>
            ) : routine.done ? (
              <span className="text-xs text-muted-foreground">{t("routines.done")}</span>
            ) : routine.next_run_at ? (
              <span className="text-xs" title={absoluteTime(routine.next_run_at, locale)}>
                {relativeTime(routine.next_run_at, locale)}
              </span>
            ) : (
              <span className="text-xs text-muted-foreground">—</span>
            )}
          </div>
          <Button
            size="sm"
            variant="secondary"
            disabled={routine.running || !!routine.queued || runMutation.isPending}
            onClick={() => runMutation.mutate()}
          >
            {routine.running
              ? t("routines.statusRunning")
              : routine.queued
                ? t("routines.statusQueued")
                : t("routines.runNow")}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** A running run's progress: the phase in words, and a bar (determinate
 * once the phase has a total, indeterminate while the provider is listing). */
function RunProgressLine({ progress, tx }: { progress: RoutineRunProgress; tx: Translate }) {
  const { label } = usePublicProviders();
  const text = progressText(tx, label, progress);
  const percent = progress.total ? Math.min(100, (progress.processed / progress.total) * 100) : undefined;
  return (
    <div className="space-y-1">
      <div className="text-xs text-info">{text}</div>
      <ProgressBar percent={percent} label={text} />
    </div>
  );
}

/** One line per provider of a run's result: its sentence, or its error. */
function ProviderReports({ routineKey, reports, tx }: { routineKey: string; reports: RoutineProviderReport[]; tx: Translate }) {
  const { label } = usePublicProviders();
  if (!reports.length) {
    const none = tx(`routines.noProviders.${routineKey}`, "");
    return none ? <div className="text-xs text-muted-foreground">{none}</div> : null;
  }
  return (
    <ul className="space-y-1">
      {reports.map((report, index) => {
        const id = report.sync_provider ?? report.provider ?? "";
        const name = id ? label(id) : "";
        return (
          <li key={`${id}-${index}`} className="text-xs break-words">
            <span className="font-medium">{name}</span>
            {report.error_code ? (
              <ErrorMessage
                className="text-destructive"
                code={report.error_code}
                message={report.error_message}
                detail={report.error_detail}
              />
            ) : (
              <span className="text-muted-foreground">
                {": "}
                {report.status === "done"
                  ? providerReportText(tx, routineKey, report)
                  : tx(`routines.providerStatus.${report.status}`, report.status)}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** The like matching ran on what the database knew: says so when the last
 * update of the library from the sync provider failed, or never ran. */
function LibraryIndexNote({ state, t }: { state: LibraryIndexState; t: RoutineRowProps["t"] }) {
  const errorText = useErrorText();
  if (state.status === "ok") return null;
  const text =
    state.status === "error"
      ? t("routines.libraryIndexFailed", { error: errorText(state.error_code, state.error_message) })
      : t("routines.libraryIndexNever");
  return (
    <div className="flex items-start gap-1.5 text-xs text-warning">
      <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0" />
      <span>{text}</span>
    </div>
  );
}
