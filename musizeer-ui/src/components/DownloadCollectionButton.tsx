import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { CheckCircle2, Download, Heart, Hourglass, Loader2 } from "lucide-react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DownloadButton } from "@/components/DownloadButton";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants, type ButtonProps } from "@/components/ui/button";
import { useCapabilities } from "@/hooks/useCapabilities";
import { useCollectionDownload, type CollectionKind } from "@/hooks/useCollectionDownload";
import { useApproval, useWaitingRequest } from "@/hooks/useDownloadApproval";
import { downloadModes, type DownloadMode } from "@/hooks/useTrackDownload";
import { useServerAction } from "@/hooks/useServerAction";
import { rememberFavorite } from "@/hooks/useTrackFavorite";
import { addFavorites } from "@/lib/api-client";
import { missingTracks } from "@/lib/download-requests";
import { useI18n } from "@/i18n";
import type { FavoritesSummary, LibraryCoverage, Track } from "@/types";

interface DownloadCollectionButtonProps {
  kind: CollectionKind;
  provider: string;
  id: string;
  /** The collection's name, for the confirmation. */
  name: string;
  /** Visible text of the primary action. */
  label: string;
  /** Name and tooltip of the download that adds to favorites (a user's only one). */
  favoriteLabel: string;
  variant?: ButtonProps["variant"];
  /** Why it cannot be used (e.g. the tracks cannot be read); disables it. */
  unavailableReason?: string;
  /** Album, playlist: queue only the tracks not in the library yet. */
  onlyMissing?: boolean;
  /** How many tracks the request would need, when the page knows (it says whether an admin has to approve it). */
  needed?: number | null;
}

/**
 * Queues a whole album, playlist or artist. A user's download adds the
 * tracks to their favorites when they arrive (and says so); an admin gets a
 * split button: the primary downloads only, the menu adds "and add to
 * favorites". An artist can mean hundreds of downloads, so it asks first.
 * When the approval policy would make the request wait, the button is a
 * "Request" that says why; once asked, it is a "Waiting for approval" state.
 * Hidden without the download capability.
 */
export function DownloadCollectionButton({
  kind,
  provider,
  id,
  name,
  label,
  favoriteLabel,
  variant = "brand",
  unavailableReason,
  onlyMissing,
  needed = null,
}: DownloadCollectionButtonProps) {
  const { t } = useI18n();
  const { can } = useCapabilities();
  const modes = downloadModes(can);
  const server = useServerAction();
  const { download, busy } = useCollectionDownload();
  const approval = useApproval(kind, needed);
  const waiting = useWaitingRequest(kind, provider, id);
  const [confirming, setConfirming] = useState<DownloadMode | null>(null);
  if (modes.length === 0) return null;
  if (waiting) return <WaitingForApproval />;

  const start = (mode: DownloadMode) => void download(kind, provider, id, mode, { onlyMissing });
  return (
    <>
      <DownloadButton
        modes={modes}
        onDownload={kind === "artist" ? setConfirming : start}
        icon={busy ? <Loader2 aria-hidden className="animate-spin" /> : <Download aria-hidden />}
        text={approval.waits ? t("approval.request") : label}
        labels={{ plain: label, favorite: approval.waits ? t("approval.requestItem", { title: name }) : favoriteLabel }}
        menuLabel={t("slot.downloadOptions", { title: name })}
        disabled={busy || server.disabled || !!unavailableReason}
        title={unavailableReason ?? server.reason ?? approval.hint}
        className={buttonVariants({ variant, size: "lg", className: "px-5" })}
        menuClassName={buttonVariants({ variant, size: "lg", className: "rounded-l-none border-l border-white/30 px-3" })}
      />
      {approval.waits && approval.hint && <span className="self-center text-sm text-muted-foreground">{approval.hint}</span>}
      {kind === "artist" && (
        <ConfirmDialog
          open={confirming !== null}
          onOpenChange={(open) => !open && setConfirming(null)}
          title={t(approval.waits ? "approval.confirmTitle" : "tile.confirmArtistTitle", { name })}
          description={t(approval.waits ? "approval.confirmBody" : "tile.confirmArtistBody")}
          confirmLabel={t(approval.waits ? "approval.confirmAction" : "tile.confirmArtistAction")}
          onConfirm={() => confirming && start(confirming)}
        />
      )}
    </>
  );
}

/** The request for this collection was sent and waits for an admin. */
function WaitingForApproval() {
  const { t } = useI18n();
  return (
    <Badge variant="warning" role="status" className="h-11 gap-1.5 rounded-md px-4 text-sm">
      <Hourglass aria-hidden className="size-4" />
      {t("approval.waiting")}
    </Badge>
  );
}

/**
 * The main action of an album or a playlist, from its library coverage:
 * download it all, download only what is missing, or nothing left to do.
 */
export function CollectionDownloadAction({
  kind,
  provider,
  id,
  name,
  library,
  unavailableReason,
}: {
  kind: "album" | "playlist";
  provider: string;
  id: string;
  name: string;
  library: LibraryCoverage | undefined;
  unavailableReason?: string;
}) {
  const { t } = useI18n();
  const { can } = useCapabilities();
  const common = { kind, provider, id, name, unavailableReason };
  // Nothing in the library yet, or no count of the tracks to say what is missing.
  if (!library || library.total === null || (library.downloaded === 0 && library.in_progress === 0)) {
    return (
      <DownloadCollectionButton
        {...common}
        label={t("album.downloadAll")}
        favoriteLabel={t("collection.downloadAllWithFavorite")}
        onlyMissing
        needed={missingTracks(library, null)}
      />
    );
  }
  // Neither in the library nor on their way.
  const missing = missingTracks(library, null) ?? 0;
  if (missing > 0) {
    return (
      <DownloadCollectionButton
        {...common}
        label={t("album.downloadMissing", { count: missing })}
        favoriteLabel={t("collection.downloadMissingWithFavorite", { count: missing })}
        onlyMissing
        needed={missing}
      />
    );
  }
  if (!can.download) return null;
  return (
    <Badge variant="secondary" role="status" className="h-11 gap-1.5 rounded-md px-4 text-sm">
      <CheckCircle2 aria-hidden className="size-4 text-success" />
      {library.in_progress > 0 ? t("album.allOnTheWay") : t("album.allInLibrary")}
    </Badge>
  );
}

/**
 * The heart of an album or a playlist: adds to the favorites its tracks
 * that are in the library ("Add the 3 in library to favorites"), with the
 * counts the server computed. Nothing when none is in the library.
 */
export function CollectionFavoriteAction({
  tracks,
  favorites,
  onChanged,
}: {
  tracks: Track[];
  favorites: FavoritesSummary | undefined;
  /** Called after the server added them (to read the new state). */
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const { can } = useCapabilities();
  const server = useServerAction();
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  if (!can.favorite || !favorites) return null;

  if (favorites.addable === 0) {
    if (favorites.favorite === 0) return null;
    return (
      <Badge variant="secondary" role="status" className="h-11 gap-1.5 rounded-md px-4 text-sm">
        <Heart aria-hidden className="size-4 text-primary" fill="currentColor" />
        {t("collection.allInFavorites", { count: favorites.favorite })}
      </Badge>
    );
  }

  const add = async () => {
    const ids = tracks.filter((track) => track.in_library && !track.favorite && track.library_id).map((track) => track.library_id as string);
    setBusy(true);
    try {
      const res = await addFavorites(ids);
      for (const id of res.added) rememberFavorite(queryClient, id, true);
      toast.success(t("collection.added", { count: res.added.length }));
      void queryClient.invalidateQueries({ queryKey: ["favorites"] });
      onChanged();
    } catch {
      toast.error(t("heart.failed"));
    } finally {
      setBusy(false);
    }
  };
  const label = t("collection.addToFavorites", { count: favorites.addable });
  return (
    <Button size="lg" variant="outline" className="px-5" onClick={() => void add()} disabled={busy || server.disabled} title={server.reason ?? label}>
      {busy ? <Loader2 aria-hidden className="animate-spin" /> : <Heart aria-hidden />}
      {label}
    </Button>
  );
}
