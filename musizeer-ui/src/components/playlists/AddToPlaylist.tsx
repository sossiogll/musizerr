import { useState, type ReactNode } from "react";
import { useMutation } from "@tanstack/react-query";
import { ListPlus } from "lucide-react";
import { toast } from "sonner";
import { addPlaylistTracks, createUserPlaylist } from "@/lib/api-client";
import { usePlaylistFailure, useRefreshPlaylists, useUserPlaylists } from "@/hooks/useUserPlaylists";
import { useI18n } from "@/i18n";
import { PlaylistNameDialog } from "@/components/playlists/PlaylistNameDialog";
import { DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import type { PlaylistTrackRef, Track, UserPlaylist } from "@/types";

/** How the server is told which track: the one it knows by its library id, else the provider's. */
export function trackRef(track: Pick<Track, "library_id" | "provider" | "provider_id">): PlaylistTrackRef {
  return track.library_id ? { track_id: track.library_id } : { provider: track.provider, provider_id: track.provider_id };
}

export interface AddToPlaylist {
  /** The entries of the "Add to playlist" submenu: the viewer's own playlists, and a new one. */
  content: ReactNode;
  /** The dialog that names a new playlist: rendered next to the menu, since the menu closes first. */
  dialog: ReactNode;
}

/**
 * "Add to playlist" for a track: the viewer's own playlists (a followed one is
 * read-only) and a new one. The playlists are asked for when the submenu
 * opens (its content is mounted then), never for every row of a list.
 */
export function useAddToPlaylist(track: Track): AddToPlaylist {
  const { t } = useI18n();
  const failure = usePlaylistFailure();
  const refresh = useRefreshPlaylists();
  const [naming, setNaming] = useState(false);

  const add = useMutation({
    mutationFn: async (target: Pick<UserPlaylist, "id" | "name"> | { name: string }) => {
      const playlist = "id" in target ? target : await createUserPlaylist(target.name);
      if (!("id" in target)) toast.success(t("playlists.created", { name: playlist.name }));
      const result = await addPlaylistTracks(playlist.id, [trackRef(track)]);
      return { playlist, result };
    },
    onSuccess: ({ playlist, result }) => {
      const key = result.added > 0 ? "playlists.added" : "playlists.alreadyIn";
      toast.success(t(key, { title: track.title, playlist: playlist.name }));
      refresh(playlist.id);
    },
    onError: (error) => {
      failure(error, t("playlists.addFailed"));
      refresh();
    },
  });

  return {
    content: <PlaylistChoices onPick={(playlist) => add.mutate(playlist)} onNew={() => setNaming(true)} />,
    dialog: (
      <PlaylistNameDialog
        open={naming}
        onOpenChange={setNaming}
        title={t("playlists.newTitle")}
        confirmLabel={t("playlists.create")}
        onConfirm={(name) => add.mutate({ name })}
      />
    ),
  };
}

function PlaylistChoices({ onPick, onNew }: { onPick: (playlist: UserPlaylist) => void; onNew: () => void }) {
  const { t } = useI18n();
  const { data, isLoading } = useUserPlaylists();
  const own = (data?.items ?? []).filter((p) => p.kind === "own");
  return (
    <>
      {isLoading && <p className="px-2 py-1.5 text-sm text-muted-foreground">{t("playlists.loadingList")}</p>}
      {!isLoading && own.length === 0 && <p className="px-2 py-1.5 text-sm text-muted-foreground">{t("playlists.noOwn")}</p>}
      {own.map((playlist) => (
        <DropdownMenuItem key={playlist.id} onSelect={() => onPick(playlist)} className="min-h-11 sm:min-h-0">
          <span className="min-w-0 flex-1 truncate">{playlist.name}</span>
          <span className="text-xs tabular-nums text-muted-foreground">{playlist.track_count}</span>
        </DropdownMenuItem>
      ))}
      <DropdownMenuSeparator />
      <DropdownMenuItem onSelect={onNew} className="min-h-11 sm:min-h-0">
        <ListPlus aria-hidden />
        {t("playlists.newInline")}
      </DropdownMenuItem>
    </>
  );
}
