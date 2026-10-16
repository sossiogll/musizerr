import { useId, useState } from "react";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Switch } from "@/components/ui/switch";
import { useI18n } from "@/i18n";
import type { UserPlaylist } from "@/types";

interface DeletePlaylistDialogProps {
  playlist: UserPlaylist;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `deleteTracks`: also delete the files that were downloaded for the playlist. Off unless the viewer turns it on. */
  onConfirm: (deleteTracks: boolean) => void;
}

/**
 * Deleting an own playlist, or unfollowing a followed one. Unfollowing asks
 * whether to also delete the tracks that were downloaded for it: the answer
 * is explicit and starts as "no" (the tracks stay in the library).
 */
export function DeletePlaylistDialog({ playlist, open, onOpenChange, onConfirm }: DeletePlaylistDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      {/* Mounted only while open: every opening starts with the switch off. */}
      <AlertDialogContent>
        <DeleteForm playlist={playlist} onConfirm={onConfirm} />
      </AlertDialogContent>
    </AlertDialog>
  );
}

function DeleteForm({ playlist, onConfirm }: Pick<DeletePlaylistDialogProps, "playlist" | "onConfirm">) {
  const { t } = useI18n();
  const [deleteTracks, setDeleteTracks] = useState(false);
  const switchId = useId();
  const hintId = useId();
  const followed = playlist.kind === "followed";

  return (
    <>
      <AlertDialogTitle>{t(followed ? "playlists.unfollowTitle" : "playlists.deleteTitle", { name: playlist.name })}</AlertDialogTitle>
      <AlertDialogDescription>{t(followed ? "playlists.unfollowBody" : "playlists.deleteBody")}</AlertDialogDescription>
      {followed && (
        <div className="flex items-start gap-3">
          <Switch id={switchId} checked={deleteTracks} onCheckedChange={setDeleteTracks} aria-describedby={hintId} className="mt-0.5" />
          <div className="space-y-1">
            <label htmlFor={switchId} className="text-sm font-medium">
              {t("playlists.deleteTracks")}
            </label>
            <p id={hintId} className="text-[13px] text-muted-foreground">
              {t("playlists.deleteTracksHint")}
            </p>
          </div>
        </div>
      )}
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
        <AlertDialogAction destructive onClick={() => onConfirm(followed && deleteTracks)}>
          {t(followed ? "playlists.unfollowConfirm" : "playlists.deleteConfirm")}
        </AlertDialogAction>
      </div>
    </>
  );
}
