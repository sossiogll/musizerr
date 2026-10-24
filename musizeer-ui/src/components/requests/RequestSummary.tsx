import { Link } from "@tanstack/react-router";
import { CoverImage } from "@/components/CoverImage";
import { Badge, type BadgeProps } from "@/components/ui/badge";
import { useRequestText } from "@/hooks/useRequestText";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import type { DownloadRequest, RequestStatus } from "@/types";

const STATUS_VARIANT: Record<RequestStatus, NonNullable<BadgeProps["variant"]>> = {
  pending: "warning",
  approved: "success",
  denied: "destructive",
  cancelled: "secondary",
};

/** The state of a request, in words and in its tone. */
export function RequestStatusBadge({ status, className }: { status: RequestStatus; className?: string }) {
  const text = useRequestText();
  return (
    <Badge variant={STATUS_VARIANT[status] ?? "secondary"} className={className}>
      {text.status(status)}
    </Badge>
  );
}

const linkClass = "block truncate font-medium hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-xs";

/** The title of a request, a link to what it asks for (the likes download leads to the likes not in the library). */
function RequestTitle({ request }: { request: DownloadRequest }) {
  const text = useRequestText();
  const name = text.name(request);
  const { provider, id } = request.target;
  switch (request.kind) {
    case "track":
      return (
        <Link to="/track/$provider/$id" params={{ provider, id }} className={linkClass}>
          {name}
        </Link>
      );
    case "album":
      return (
        <Link to="/album/$provider/$id" params={{ provider, id }} className={linkClass}>
          {name}
        </Link>
      );
    case "artist":
      return (
        <Link to="/artist/$provider/$id" params={{ provider, id }} className={linkClass}>
          {name}
        </Link>
      );
    case "playlist":
      return (
        <Link to="/playlist/$provider/$id" params={{ provider, id }} className={linkClass}>
          {name}
        </Link>
      );
    default:
      return (
        <Link to="/favorites" search={{ view: "elsewhere" }} className={linkClass}>
          {name}
        </Link>
      );
  }
}

/**
 * What a request is, the same in the user's list and the admin's: its cover,
 * its title (a link to it), the artist, the kind, how many tracks it needs
 * and the reason the policy gave. `children` is the line the list adds (who,
 * when).
 */
export function RequestSummary({ request, className, children }: { request: DownloadRequest; className?: string; children?: React.ReactNode }) {
  const { t } = useI18n();
  const text = useRequestText();
  return (
    <div className={cn("flex min-w-0 items-center gap-3", className)}>
      <CoverImage src={request.cover_url} alt="" type={request.kind === "liked_elsewhere" ? "track" : request.kind} size="md" />
      <div className="min-w-0 space-y-0.5">
        <RequestTitle request={request} />
        {request.subtitle && <p className="truncate text-sm text-muted-foreground">{request.subtitle}</p>}
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
          <Badge variant="outline">{text.kind(request.kind)}</Badge>
          <span>{t("requests.neededTracks", { count: request.needed_tracks })}</span>
          {request.status === "approved" && request.queued_tracks !== null && <span>{t("requests.queued", { count: request.queued_tracks })}</span>}
          <span>{text.reason(request.reason)}</span>
        </div>
        {children}
      </div>
    </div>
  );
}
