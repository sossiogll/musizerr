import { memo } from "react";
import { AlertTriangle, CheckCircle2, Clock, Download, Hourglass, Loader2, RotateCcw } from "lucide-react";
import type { DownloadTone } from "@/lib/api-client";
import type { Approval } from "@/hooks/useDownloadApproval";
import type { DownloadMode, SlotState } from "@/hooks/useTrackDownload";
import { DownloadButton } from "@/components/DownloadButton";
import { useDownloadStates } from "@/hooks/useDownloadStates";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

const TONE: Record<DownloadTone, string> = {
  neutral: "text-muted-foreground",
  info: "text-primary",
  success: "text-success",
  warning: "text-warning",
  danger: "text-destructive",
};

// Fixed width: the slot never moves the rest of the row. Icon + word from
// M up, icon only on S/XS, always a 44px touch target.
const SLOT = "flex h-11 w-11 shrink-0 items-center justify-center md:w-32 md:justify-start";
const LABEL = "max-md:sr-only truncate";
const BUTTON = cn(
  "inline-flex h-11 min-w-11 items-center justify-center gap-1.5 rounded-md text-sm font-medium transition-colors md:h-8 md:border md:px-3",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-50",
);
const NEUTRAL = "text-foreground/90 hover:bg-muted md:border-border";
const DANGER = "text-destructive md:border-destructive/40 md:bg-destructive/10 hover:bg-destructive/15";

interface StatusSlotProps {
  state: SlotState;
  title: string;
  /** The downloads the viewer may start, primary first. */
  modes: DownloadMode[];
  onDownload: (mode: DownloadMode) => void;
  busy?: boolean;
  /** Set when the action cannot run now (e.g. the server is unreachable). */
  disabledReason?: string;
  /** Percent of the transfer, when the server reports it. */
  percent?: number;
  /** Whether a download would wait for an admin: the button then says "Request". */
  approval?: Approval;
}

/**
 * The FILE state of a track, always in the same place: not in library
 * (Download, or Request when an admin has to approve it, or "Awaiting
 * approval" once asked), queued, downloading, in library, needs review
 * (Retry) — plus "needs a decision" for a conflict. Label and tone come from
 * the backend's download-state vocabulary; the phase decides the shape.
 */
export const StatusSlot = memo(function StatusSlot({ state, title, modes, onDownload, busy, disabledReason, percent, approval }: StatusSlotProps) {
  const { t } = useI18n();
  const { label } = useDownloadStates();

  if (state.kind === "none") return <span className={SLOT} aria-hidden />;

  if (state.kind === "awaiting") {
    return (
      <span className={cn(SLOT, "gap-1.5 text-sm text-warning")} role="status" title={t("approval.waiting")}>
        <Hourglass aria-hidden className="size-4 shrink-0" />
        <span className={LABEL}>{t("approval.waitingShort")}</span>
      </span>
    );
  }

  if (state.kind === "available") {
    const waits = approval?.waits ?? false;
    return (
      <span className={SLOT}>
        <DownloadButton
          // The slot's own button is the primary download; the other (an admin's
          // "and add to favorites") is in the row's menu.
          modes={modes.slice(0, 1)}
          onDownload={onDownload}
          icon={busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : <Download aria-hidden className="size-4" />}
          text={t(waits ? "approval.request" : "slot.download")}
          labels={{
            plain: t("slot.downloadPlainTrack", { title }),
            favorite: t(waits ? "approval.requestItem" : "slot.downloadWithFavoriteTrack", { title }),
          }}
          menuLabel={t("slot.downloadOptions", { title })}
          disabled={busy || !!disabledReason}
          title={disabledReason ?? approval?.hint}
          className={cn(BUTTON, NEUTRAL)}
          compact
        />
      </span>
    );
  }

  const text = label(state.status);
  const tone = TONE[state.meta?.tone ?? "neutral"];

  switch (state.phase) {
    case "failed":
      return (
        <span className={SLOT}>
          <SlotButton
            icon={busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : <RotateCcw aria-hidden className="size-4" />}
            text={t("slot.retry")}
            ariaLabel={t("slot.retryTrack", { title })}
            // A retry keeps what was asked: a user's always adds to favorites.
            onClick={() => onDownload(modes[modes.length - 1] ?? "favorite")}
            disabled={busy || !!disabledReason}
            title={disabledReason ?? text}
            danger
          />
        </span>
      );
    case "active":
      return (
        <span className={cn(SLOT, "text-sm", tone)} role="status" title={text}>
          <Loader2 aria-hidden className="size-4 shrink-0 animate-spin md:hidden" />
          <span className="hidden min-w-0 flex-1 flex-col gap-1 md:flex">
            <span className="truncate text-xs">
              {percent !== undefined ? t("slot.progress", { label: text, percent: Math.round(percent) }) : text}
            </span>
            <span aria-hidden className="h-[3px] overflow-hidden rounded-xs bg-muted">
              <span
                className={cn("block h-full rounded-xs bg-primary", percent === undefined && "w-1/3 animate-[slot-indeterminate_1.4s_ease-in-out_infinite]")}
                style={percent !== undefined ? { width: `${percent}%` } : undefined}
              />
            </span>
          </span>
          <span className="sr-only md:hidden">{text}</span>
        </span>
      );
    default: {
      const Icon = state.phase === "done" ? CheckCircle2 : state.phase === "review" ? AlertTriangle : Clock;
      return (
        <span className={cn(SLOT, "gap-1.5 text-sm", tone)} title={text}>
          <Icon aria-hidden className="size-4 shrink-0" />
          <span className={LABEL}>{text}</span>
        </span>
      );
    }
  }
});

function SlotButton({
  icon,
  text,
  ariaLabel,
  onClick,
  disabled,
  title,
  danger,
}: {
  icon: React.ReactNode;
  text: string;
  ariaLabel: string;
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      title={title}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      disabled={disabled}
      className={cn(BUTTON, danger ? DANGER : NEUTRAL)}
    >
      {icon}
      <span className={LABEL}>{text}</span>
    </button>
  );
}
