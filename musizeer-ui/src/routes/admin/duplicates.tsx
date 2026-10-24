import { useCallback, useId, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CopyX, Loader2, Pause, Play } from "lucide-react";
import { toast } from "sonner";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { FilterTabs } from "@/components/FilterTabs";
import { useProviderLabel } from "@/components/ProviderBadges";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { usePlayer, type PlayerTrack } from "@/contexts/PlayerContext";
import { useServerAction } from "@/hooks/useServerAction";
import {
  dismissDuplicateGroup,
  getDuplicateGroups,
  resolveAllDuplicateGroups,
  resolveDuplicateGroup,
  type DuplicateGroup,
  type DuplicateGroupStatus,
  type DuplicateMember,
} from "@/lib/api-client";
import { useI18n, type MessageKey } from "@/i18n";
import { parseApiDate } from "@/lib/dates";
import { cn, formatBytes, formatDuration } from "@/lib/utils";

const STATUSES: readonly DuplicateGroupStatus[] = ["proposed", "needs_review", "resolved", "dismissed"];
const OPEN: ReadonlySet<DuplicateGroupStatus> = new Set(["proposed", "needs_review"]);

/** Administration › Duplicates: the groups of GET /library/duplicates. */
export function AdminDuplicatesPage() {
  const [status, setStatus] = useState<DuplicateGroupStatus>("proposed");
  const [confirmAll, setConfirmAll] = useState(false);
  const queryClient = useQueryClient();
  const server = useServerAction();
  const { t } = useI18n();

  const { data: groups, isLoading } = useQuery({
    queryKey: ["duplicates", status],
    queryFn: () => getDuplicateGroups(status),
  });

  const resolveAll = useMutation({
    mutationFn: resolveAllDuplicateGroups,
    onSuccess: () => {
      toast.success(t("duplicates.resolved"));
      void queryClient.invalidateQueries({ queryKey: ["duplicates"] });
    },
    onError: () => toast.error(t("duplicates.actionFailed")),
  });

  const tabs = STATUSES.map((value) => ({ value, label: t(`duplicates.status_${value}` as MessageKey) }));

  return (
    <div className="space-y-4">
      <p className="max-w-prose text-sm text-muted-foreground">{t("duplicates.intro")}</p>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <FilterTabs tabs={tabs} value={status} onChange={setStatus} label={t("duplicates.filter")} />
        {status === "proposed" && !!groups?.length && (
          <Button
            variant="outline"
            onClick={() => setConfirmAll(true)}
            disabled={server.disabled || resolveAll.isPending}
            title={server.reason}
          >
            {resolveAll.isPending && <Loader2 aria-hidden className="animate-spin" />}
            {t("duplicates.resolveAll")}
          </Button>
        )}
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      ) : !groups?.length ? (
        <div className="flex flex-col items-center gap-2 py-12 text-muted-foreground">
          <CopyX aria-hidden className="size-8 opacity-50" />
          <p className="text-sm">{t("duplicates.empty")}</p>
        </div>
      ) : (
        <ul className="space-y-3">
          {groups.map((group) => (
            <li key={group.id}>
              <GroupCard group={group} />
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={confirmAll}
        onOpenChange={setConfirmAll}
        title={t("duplicates.confirmAllTitle")}
        description={t("duplicates.confirmAllBody")}
        confirmLabel={t("duplicates.resolveAll")}
        destructive
        onConfirm={() => resolveAll.mutate()}
      />
    </div>
  );
}

/** The path that names a member: where it really is, else the sync provider's label. */
const shownPath = (member: DuplicateMember) => member.storage_path ?? member.path;

function GroupCard({ group }: { group: DuplicateGroup }) {
  // Only the server's keeper is preselected: a group without one (needs
  // review) starts with nothing chosen, so the primary action never deletes
  // a file nobody picked.
  const [keep, setKeep] = useState(() => group.members.find((m) => m.is_keeper)?.id ?? "");
  const [confirming, setConfirming] = useState(false);
  const queryClient = useQueryClient();
  const server = useServerAction();
  const { t, tx } = useI18n();
  const open = OPEN.has(group.status);
  const head = group.members[0];

  // What "resolve" would delete: every other member Musizeer downloaded.
  const toDelete = useMemo(() => group.members.filter((m) => m.id !== keep && m.deletable && !m.deleted_at), [group.members, keep]);

  // Nothing to delete (every other file was added outside Musizeer, or the
  // one kept is the only one Musizeer downloaded): resolving would only
  // close the group, so it is offered as that, never as a deletion. When no
  // file can be deleted at all, any of them can be the one "kept".
  const nothingDeletable = !group.members.some((m) => m.deletable && !m.deleted_at);
  const keeper = keep || (nothingDeletable ? (head?.id ?? "") : "");
  const deletesNothing = toDelete.length === 0;

  const done = () => void queryClient.invalidateQueries({ queryKey: ["duplicates"] });
  const resolve = useMutation({
    mutationFn: () => resolveDuplicateGroup(group.id, keeper),
    onSuccess: () => {
      toast.success(t(deletesNothing ? "duplicates.handled" : "duplicates.resolved"));
      done();
    },
    onError: () => toast.error(t("duplicates.actionFailed")),
  });
  const dismiss = useMutation({
    mutationFn: () => dismissDuplicateGroup(group.id),
    onSuccess: () => {
      toast.success(t("duplicates.dismissed"));
      done();
    },
    onError: () => toast.error(t("duplicates.actionFailed")),
  });
  const busy = resolve.isPending || dismiss.isPending;
  const reasonId = useId();

  return (
    <article className="space-y-3 rounded-lg border bg-card p-4">
      <header className="min-w-0 space-y-1">
        <h2 className="truncate font-medium">{head?.title}</h2>
        <p className="truncate text-sm text-muted-foreground">{[head?.artist, head?.album].filter(Boolean).join(" · ")}</p>
        {group.reason && (
          <p id={reasonId} className="max-w-prose text-sm">
            <span className="font-medium">{t("duplicates.reasonLabel", { reason: tx(`duplicates.reason.${group.reason}`, group.reason) })}</span>{" "}
            <span className="text-muted-foreground">{tx(`duplicates.reasonHelp.${group.reason}`, "")}</span>
          </p>
        )}
      </header>

      <fieldset disabled={!open} className="space-y-2" aria-describedby={group.reason ? reasonId : undefined}>
        <legend className="sr-only">{t("duplicates.keep")}</legend>
        {group.members.map((member) => (
          <MemberRow key={member.id} member={member} groupId={group.id} checked={keep === member.id} onKeep={setKeep} />
        ))}
      </fieldset>

      {group.deleted_paths.length > 0 && (
        <p className="break-all text-xs text-muted-foreground">{t("duplicates.deletedPaths", { paths: group.deleted_paths.join(", ") })}</p>
      )}

      {open && nothingDeletable && <p className="text-sm text-muted-foreground">{t("duplicates.nothingDeletable")}</p>}
      {open && !keeper && <p className="text-sm text-muted-foreground">{t("duplicates.chooseKeeper")}</p>}

      {open && (
        <div className="flex flex-wrap gap-2">
          <Button
            onClick={() => (deletesNothing ? resolve.mutate() : setConfirming(true))}
            disabled={server.disabled || busy || !keeper}
            title={server.reason}
          >
            {resolve.isPending && <Loader2 aria-hidden className="animate-spin" />}
            {t(deletesNothing ? "duplicates.markHandled" : "duplicates.resolve")}
          </Button>
          <Button variant="outline" onClick={() => dismiss.mutate()} disabled={server.disabled || busy} title={server.reason}>
            {t("duplicates.dismiss")}
          </Button>
        </div>
      )}

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={t("duplicates.confirmResolveTitle", { count: toDelete.length })}
        description={t("duplicates.confirmResolveBody", { paths: toDelete.map(shownPath).join(", ") })}
        confirmLabel={t("duplicates.resolve")}
        destructive
        onConfirm={() => resolve.mutate()}
      />
    </article>
  );
}

/** Format, quality, size and duration of a member: what tells two copies apart. */
function useMemberDetails() {
  const { t, locale } = useI18n();
  return useCallback(
    (member: DuplicateMember): string[] =>
      [
        member.suffix?.toUpperCase(),
        member.lossless ? t("duplicates.lossless") : member.bitrate ? `${Math.round(member.bitrate / 1000)} kbps` : null,
        member.sample_rate ? t("duplicates.sampleRate", { khz: (member.sample_rate / 1000).toLocaleString(locale) }) : null,
        member.size ? formatBytes(member.size, locale) : null,
        member.duration_ms ? formatDuration(member.duration_ms) : null,
      ].filter((part): part is string => !!part),
    [t, locale],
  );
}

function MemberRow({
  member,
  groupId,
  checked,
  onKeep,
}: {
  member: DuplicateMember;
  groupId: string;
  checked: boolean;
  onKeep: (id: string) => void;
}) {
  const { t, locale } = useI18n();
  const providerLabel = useProviderLabel();
  const details = useMemberDetails()(member);
  const path = shownPath(member);
  const origin =
    member.origin !== "musizeer"
      ? t("duplicates.origin_unknown")
      : member.downloaded_via
        ? t("duplicates.originVia", { provider: providerLabel(member.downloaded_via) })
        : t("duplicates.origin_musizeer");

  return (
    <div
      className={cn(
        "flex items-start gap-2 rounded-md border p-2 text-sm",
        checked ? "border-primary/60 bg-primary/5" : "hover:bg-muted/50",
        member.deleted_at && "opacity-50",
      )}
    >
      <MemberPlayButton member={member} path={path} details={details} />
      <label className="flex min-w-0 flex-1 cursor-pointer items-start gap-3 py-1">
        <input
          type="radio"
          name={`keep-${groupId}`}
          className="mt-1 size-4 shrink-0 accent-[var(--color-primary)]"
          checked={checked}
          onChange={() => onKeep(member.id)}
        />
        <span className="min-w-0 flex-1 space-y-1">
          <span className="block break-all font-mono text-xs" title={member.storage_path ? t("duplicates.storagePath") : t("duplicates.syncPathOnly")}>
            {path}
          </span>
          {member.storage_path && member.storage_path !== member.path && (
            <span className="block break-all text-xs text-muted-foreground">{t("duplicates.syncPath", { path: member.path })}</span>
          )}
          <span className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
            <span>{details.join(" · ")}</span>
            <span>{origin}</span>
            {member.added_at && <span>{t("duplicates.addedAt", { date: parseApiDate(member.added_at).toLocaleDateString(locale) })}</span>}
            {member.fingerprint_score !== null && (
              <span>{t("duplicates.match", { score: Math.round(member.fingerprint_score * 100) })}</span>
            )}
            {member.best_quality && (
              <span className="font-medium text-foreground" title={t("duplicates.bestQualityHint")}>
                {t("duplicates.bestQuality")}
              </span>
            )}
            {member.is_keeper && <span className="font-medium text-success">{t("duplicates.keeper")}</span>}
          </span>
        </span>
      </label>
    </div>
  );
}

/**
 * Plays THIS file (its own stream, never the track's) on its own: the
 * player says which file it is and goes back to the queue afterwards.
 */
function MemberPlayButton({ member, path, details }: { member: DuplicateMember; path: string; details: string[] }) {
  const { t } = useI18n();
  const { track, playing, playOnce } = usePlayer();
  if (!member.stream_url) return <span aria-hidden className="size-11 shrink-0" />;
  const current = !!track?.detached && track.provider_id === member.id;
  const active = current && playing;
  return (
    <button
      type="button"
      aria-pressed={active}
      aria-label={t(active ? "duplicates.pauseFile" : "duplicates.playFile", { path })}
      title={t(active ? "duplicates.pauseFile" : "duplicates.playFile", { path })}
      onClick={() => playOnce(memberTrack(member, t("duplicates.playingNote", { details: details.join(" · ") })))}
      className={cn(
        "grid size-11 shrink-0 place-items-center rounded-md transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        current ? "text-primary" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {active ? <Pause aria-hidden className="size-5" /> : <Play aria-hidden className="size-5" />}
    </button>
  );
}

/** One file of a group, as the player holds it: its own stream, and a note saying which file it is. */
function memberTrack(member: DuplicateMember, note: string): PlayerTrack {
  return {
    title: member.title,
    artist: member.artist,
    cover_url: null,
    preview_url: null,
    stream_url: member.stream_url,
    playback: { type: "full", source: "library" },
    provider: "",
    provider_id: member.id,
    artist_provider_id: null,
    detached: { note },
  };
}
