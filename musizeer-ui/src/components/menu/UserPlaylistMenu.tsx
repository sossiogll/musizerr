import { useState, type ReactNode } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Copy, ListEnd, ListMusic, Pencil, Play, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { ActionMenu, type MenuEntries, type MenuParts } from "@/components/menu/ActionMenu";
import { DeletePlaylistDialog } from "@/components/playlists/DeletePlaylistDialog";
import { PlaylistNameDialog } from "@/components/playlists/PlaylistNameDialog";
import { usePlayTracks } from "@/hooks/usePlayTracks";
import { useServerAction } from "@/hooks/useServerAction";
import { usePlaylistFailure, useRefreshPlaylists } from "@/hooks/useUserPlaylists";
import { useI18n } from "@/i18n";
import { deleteUserPlaylist, duplicateUserPlaylist, getUserPlaylist, updateUserPlaylist } from "@/lib/api-client";
import { queryKeys } from "@/lib/query-keys";
import type { UserPlaylist } from "@/types";

interface UserPlaylistMenuProps {
  playlist: UserPlaylist;
  /** Offer "Open": on the list of playlists, not on the page of the playlist itself. */
  openable?: boolean;
  /** The playlist was deleted (or unfollowed): e.g. leave its page. */
  onDeleted?: () => void;
  buttonClassName?: string;
  children: (parts: MenuParts) => ReactNode;
}

/**
 * The menu of one of the viewer's playlists, own or followed: play it, queue
 * it, rename it (an own one), duplicate it, delete it — or, for a followed
 * one, unfollow it, asking whether to also delete the tracks that were
 * downloaded for it (the answer starts as "keep them"). The same menu on the
 * list of playlists and on the page of each.
 */
export function UserPlaylistMenu({ playlist, openable = false, onDeleted, buttonClassName, children }: UserPlaylistMenuProps) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const server = useServerAction();
  const failure = usePlaylistFailure();
  const refresh = useRefreshPlaylists();
  const { playAll, queueAll } = usePlayTracks();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const followed = playlist.kind === "followed";

  const read = async () =>
    (await queryClient.fetchQuery({ queryKey: queryKeys.userPlaylist(playlist.id), queryFn: () => getUserPlaylist(playlist.id), staleTime: 30_000 })).tracks;

  const rename = useMutation({
    mutationFn: (name: string) => updateUserPlaylist(playlist.id, { name }),
    onSuccess: () => refresh(playlist.id),
    onError: (e) => failure(e, t("playlists.renameFailed")),
  });
  const duplicate = useMutation({
    mutationFn: () => duplicateUserPlaylist(playlist.id, t("playlists.copyName", { name: playlist.name })),
    onSuccess: (copy) => {
      refresh();
      toast.success(t("playlists.duplicated", { name: copy.name }));
      void navigate({ to: "/playlists/$id", params: { id: copy.id } });
    },
    onError: (e) => failure(e, t("playlists.duplicateFailed")),
  });
  const del = useMutation({
    mutationFn: (deleteTracks: boolean) => deleteUserPlaylist(playlist.id, { deleteTracks }),
    onSuccess: (outcome) => {
      refresh(playlist.id);
      toast.success(t(followed ? "playlists.unfollowed" : "playlists.deleted", { name: playlist.name }));
      if (outcome.tracks_scheduled > 0) toast.info(t("playlists.deletingTracks", { count: outcome.tracks_scheduled }));
      onDeleted?.();
    },
    onError: (e) => failure(e, t("playlists.deleteFailed")),
  });

  const empty = playlist.track_count === 0;
  const entries: MenuEntries = [
    { type: "item", id: "play", label: t("menu.play"), icon: <Play aria-hidden />, onSelect: () => void playAll(read, playlist.name), disabled: empty },
    { type: "item", id: "queue", label: t("menu.addToQueue"), icon: <ListEnd aria-hidden />, onSelect: () => void queueAll(read, playlist.name), disabled: empty },
    { type: "separator" },
    !followed && { type: "item", id: "rename", label: t("playlists.rename"), icon: <Pencil aria-hidden />, onSelect: () => setRenaming(true), disabled: server.disabled, hint: server.reason },
    { type: "item", id: "duplicate", label: t("playlists.duplicate"), icon: <Copy aria-hidden />, onSelect: () => duplicate.mutate(), disabled: server.disabled || duplicate.isPending, hint: server.reason },
    openable && { type: "separator" },
    openable && { type: "item", id: "open", label: t("menu.goToPlaylist"), icon: <ListMusic aria-hidden />, onSelect: () => void navigate({ to: "/playlists/$id", params: { id: playlist.id } }) },
    { type: "separator" },
    {
      type: "item",
      id: "delete",
      label: t(followed ? "playlists.unfollow" : "playlists.delete"),
      icon: <Trash2 aria-hidden />,
      onSelect: () => setDeleting(true),
      disabled: server.disabled,
      hint: server.reason,
      destructive: true,
    },
  ];

  return (
    <>
      <ActionMenu entries={entries} label={t("menu.moreFor", { title: playlist.name })} heading={playlist.name} buttonClassName={buttonClassName}>
        {children}
      </ActionMenu>
      {!followed && (
        <PlaylistNameDialog
          open={renaming}
          onOpenChange={setRenaming}
          title={t("playlists.renameTitle")}
          confirmLabel={t("playlists.save")}
          initialName={playlist.name}
          onConfirm={(name) => rename.mutate(name)}
        />
      )}
      <DeletePlaylistDialog playlist={playlist} open={deleting} onOpenChange={setDeleting} onConfirm={(deleteTracks) => del.mutate(deleteTracks)} />
    </>
  );
}
