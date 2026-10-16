import { useState, type ReactNode } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { BookmarkCheck, BookmarkPlus, Disc3, Download, Hourglass, ListEnd, ListMusic, Mic2, Play } from "lucide-react";
import { ActionMenu, type MenuEntries, type MenuParts } from "@/components/menu/ActionMenu";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DeletePlaylistDialog } from "@/components/playlists/DeletePlaylistDialog";
import { useCapabilities } from "@/hooks/useCapabilities";
import { useCollectionDownload } from "@/hooks/useCollectionDownload";
import { useApproval, useWaitingRequest } from "@/hooks/useDownloadApproval";
import { useFollowPlaylist } from "@/hooks/useFollowPlaylist";
import { usePlayTracks } from "@/hooks/usePlayTracks";
import { DISCOGRAPHY_CAPABILITY, usePublicProviders } from "@/hooks/usePublicProviders";
import { useServerAction } from "@/hooks/useServerAction";
import { downloadModes, type DownloadMode } from "@/hooks/useTrackDownload";
import { useI18n } from "@/i18n";
import { getAlbum, getArtistTopTracks, getPlaylist } from "@/lib/api-client";
import { missingTracks } from "@/lib/download-requests";
import type { Album, Artist, Playlist, Track } from "@/types";

/** An album, an artist or a playlist of a metadata provider: what a tile shows. */
export type CollectionItem = Album | Artist | Playlist;

/** How long a collection the menu read stays fresh: playing it again does not ask again. */
const FRESH_MS = 60_000;

const titleOf = (item: CollectionItem) => (item.kind === "album" ? item.title : item.name);

interface CollectionMenuProps {
  item: CollectionItem;
  /** Offer the download entries (they are hidden anyway without the download capability). */
  downloadable?: boolean;
  buttonClassName?: string;
  /** `waiting`: the viewer's request to download it is with an admin (a tile shows it). */
  children: (parts: MenuParts & { waiting: boolean }) => ReactNode;
}

/**
 * The menu of an album, an artist or a playlist of a provider: play it, add
 * it to the queue, download it, follow it (a playlist), go to its page. The
 * tracks are read when one of the first two is chosen, from the same queries
 * the pages use. Downloading a whole artist (the discography, only where its
 * provider gives one) asks for confirmation first; every download is labelled
 * by the approval policy ("Request" when an admin has to approve it) and
 * says what it adds to the favorites, like the buttons of the pages.
 */
export function CollectionMenu({ item, downloadable = false, buttonClassName, children }: CollectionMenuProps) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { can } = useCapabilities();
  const providers = usePublicProviders();
  const server = useServerAction();
  const { playAll, queueAll } = usePlayTracks();
  const { download, busy } = useCollectionDownload();
  const [confirming, setConfirming] = useState<DownloadMode | null>(null);
  const [unfollowing, setUnfollowing] = useState(false);
  const title = titleOf(item);
  const { kind, provider, provider_id: id } = item;

  const modes = downloadModes(can);
  const approval = useApproval(kind, item.kind === "artist" ? null : missingTracks(item.library, item.track_count));
  const waiting = useWaitingRequest(kind, provider, id);
  const following = useFollowPlaylist({ provider, id, name: title }, kind === "playlist");
  // Whole artists only where the provider gives a discography to download.
  const canRequest = downloadable && modes.length > 0 && (kind !== "artist" || providers.can(provider, DISCOGRAPHY_CAPABILITY));

  const read = async (): Promise<Track[]> => {
    if (kind === "album") return (await queryClient.fetchQuery({ queryKey: ["album", provider, id], queryFn: () => getAlbum(provider, id), staleTime: FRESH_MS })).tracks;
    if (kind === "playlist") return (await queryClient.fetchQuery({ queryKey: ["playlist", provider, id], queryFn: () => getPlaylist(provider, id), staleTime: FRESH_MS })).tracks;
    return (await queryClient.fetchQuery({ queryKey: ["artist-top-tracks", provider, id], queryFn: () => getArtistTopTracks(provider, id), staleTime: FRESH_MS })).items;
  };

  const start = (mode: DownloadMode) => void download(kind, provider, id, mode);
  const downloadLabel = (mode: DownloadMode) =>
    approval.waits ? t("menu.requestDownload") : t(mode === "favorite" ? `menu.download.${kind}Favorite` : `menu.download.${kind}`);

  const downloadEntries: MenuEntries = !canRequest
    ? []
    : waiting
      ? [{ type: "item", id: "waiting", label: t("approval.waiting"), icon: <Hourglass aria-hidden />, onSelect: () => {}, disabled: true }]
      : modes.map((mode) => ({
          type: "item" as const,
          id: `download-${mode}`,
          label: downloadLabel(mode),
          icon: <Download aria-hidden />,
          onSelect: () => (kind === "artist" ? setConfirming(mode) : start(mode)),
          disabled: busy || server.disabled,
          hint: server.reason ?? approval.hint,
        }));

  const goTo =
    kind === "album"
      ? { label: t("menu.goToAlbum"), icon: <Disc3 aria-hidden />, to: "/album/$provider/$id" as const }
      : kind === "artist"
        ? { label: t("menu.goToArtist"), icon: <Mic2 aria-hidden />, to: "/artist/$provider/$id" as const }
        : { label: t("menu.goToPlaylist"), icon: <ListMusic aria-hidden />, to: "/playlist/$provider/$id" as const };

  const followEntries: MenuEntries = [];
  if (kind === "playlist" && following.can) {
    const mine = following.followed;
    if (mine) {
      followEntries.push(
        { type: "item", id: "open-mine", label: t("playlists.openMine"), icon: <BookmarkCheck aria-hidden />, onSelect: () => void navigate({ to: "/playlists/$id", params: { id: mine.id } }) },
        { type: "item", id: "unfollow", label: t("playlists.unfollow"), onSelect: () => setUnfollowing(true), disabled: server.disabled, destructive: true },
      );
    } else {
      followEntries.push({
        type: "item",
        id: "follow",
        label: t("playlists.follow"),
        icon: <BookmarkPlus aria-hidden />,
        hint: server.reason ?? t("playlists.followHint"),
        onSelect: () => following.follow.mutate(),
        disabled: server.disabled || following.follow.isPending,
      });
    }
  }

  const popular = kind === "artist" ? t("menu.popularHint") : undefined;
  const entries: MenuEntries = [
    { type: "item", id: "play", label: t("menu.play"), icon: <Play aria-hidden />, hint: popular, onSelect: () => void playAll(read, title) },
    { type: "item", id: "queue", label: t("menu.addToQueue"), icon: <ListEnd aria-hidden />, hint: popular, onSelect: () => void queueAll(read, title) },
    { type: "separator" },
    ...followEntries,
    ...downloadEntries,
    { type: "separator" },
    { type: "item", id: "go", label: goTo.label, icon: goTo.icon, onSelect: () => void navigate({ to: goTo.to, params: { provider, id } }) },
  ];

  return (
    <>
      <ActionMenu entries={entries} label={t("menu.moreFor", { title })} heading={title} buttonClassName={buttonClassName}>
        {(parts) => children({ ...parts, waiting: canRequest && !!waiting })}
      </ActionMenu>
      {kind === "artist" && (
        <ConfirmDialog
          open={confirming !== null}
          onOpenChange={(open) => !open && setConfirming(null)}
          title={t(approval.waits ? "approval.confirmTitle" : "tile.confirmArtistTitle", { name: title })}
          description={t(approval.waits ? "approval.confirmBody" : "tile.confirmArtistBody")}
          confirmLabel={t(approval.waits ? "approval.confirmAction" : "tile.confirmArtistAction")}
          onConfirm={() => confirming && start(confirming)}
        />
      )}
      {following.followed && (
        <DeletePlaylistDialog playlist={following.followed} open={unfollowing} onOpenChange={setUnfollowing} onConfirm={(deleteTracks) => following.unfollow.mutate(deleteTracks)} />
      )}
    </>
  );
}
