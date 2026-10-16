import type { ReactNode } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Disc3, Download, Heart, Hourglass, ListEnd, ListPlus, ListStart, Mic2, RotateCcw, X } from "lucide-react";
import { toast } from "sonner";
import { ActionMenu, type MenuEntries, type MenuParts } from "@/components/menu/ActionMenu";
import { useAddToPlaylist } from "@/components/playlists/AddToPlaylist";
import { isPlayable, toPlayerTrack, usePlayer } from "@/contexts/PlayerContext";
import { useCan } from "@/hooks/useCapabilities";
import { useServerAction } from "@/hooks/useServerAction";
import type { HeartState } from "@/hooks/useTrackFavorite";
import type { TrackDownload } from "@/hooks/useTrackDownload";
import { useI18n } from "@/i18n";
import type { Track } from "@/types";

interface TrackMenuProps {
  track: Track;
  /** The row's own download state: the menu starts the same download the slot shows. */
  download: TrackDownload;
  heart: HeartState;
  /** Inside a playlist the viewer edits: takes the track out of it. */
  onRemove?: () => void;
  buttonClassName?: string;
  children: (parts: MenuParts) => ReactNode;
}

/**
 * The menu of a track: play next, add to the queue, add to a playlist,
 * favorite, download, go to its album or its artist, and (in an own playlist)
 * remove it. What is offered follows what the viewer may do — the
 * capabilities and download policy the server reported, and the state the
 * server gave the track — never anything derived here; the server still
 * judges every action.
 */
export function TrackMenu({ track, download, heart, onRemove, buttonClassName, children }: TrackMenuProps) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { playNext, addToQueue } = usePlayer();
  const canPlaylists = useCan("playlists");
  const server = useServerAction();
  const playlist = useAddToPlaylist(track);
  const { slot, modes, approval, busy, disabledReason } = download;
  const playable = isPlayable(track);

  const downloadEntries: MenuEntries = [];
  if (slot.kind === "available") {
    for (const mode of modes) {
      downloadEntries.push({
        type: "item",
        id: `download-${mode}`,
        label: approval.waits ? t("menu.requestDownload") : t(mode === "plain" ? "slot.download" : "slot.downloadWithFavorite"),
        icon: <Download aria-hidden />,
        onSelect: () => download.download(mode),
        disabled: busy || !!disabledReason,
        hint: disabledReason ?? approval.hint,
      });
    }
  } else if (slot.kind === "state" && slot.phase === "failed" && modes.length > 0) {
    downloadEntries.push({
      type: "item",
      id: "retry",
      label: t("slot.retry"),
      icon: <RotateCcw aria-hidden />,
      // A retry keeps what was asked: a user's always adds to favorites.
      onSelect: () => download.download(modes[modes.length - 1]),
      disabled: busy || !!disabledReason,
      hint: disabledReason,
    });
  } else if (slot.kind === "awaiting") {
    downloadEntries.push({ type: "item", id: "awaiting", label: t("approval.waiting"), icon: <Hourglass aria-hidden />, onSelect: () => {}, disabled: true });
  }

  const entries: MenuEntries = [
    playable && {
      type: "item",
      id: "play-next",
      label: t("menu.playNext"),
      icon: <ListStart aria-hidden />,
      onSelect: () => {
        playNext(toPlayerTrack(track));
        toast.success(t("menu.addedNext", { title: track.title }));
      },
    },
    playable && {
      type: "item",
      id: "queue",
      label: t("menu.addToQueue"),
      icon: <ListEnd aria-hidden />,
      onSelect: () => {
        addToQueue(toPlayerTrack(track));
        toast.success(t("menu.addedToQueue", { title: track.title }));
      },
    },
    { type: "separator" },
    canPlaylists && { type: "submenu", id: "playlist", label: t("playlists.addMenu"), icon: <ListPlus aria-hidden />, content: playlist.content, disabled: server.disabled },
    heart.kind === "heart" && {
      type: "item",
      id: "favorite",
      label: t(heart.favorite ? "menu.unfavorite" : "menu.favorite"),
      icon: <Heart aria-hidden fill={heart.favorite ? "currentColor" : "none"} />,
      onSelect: heart.toggle,
      disabled: heart.busy,
    },
    ...downloadEntries,
    { type: "separator" },
    !!track.album && !!track.album_provider_id && {
      type: "item",
      id: "album",
      label: t("menu.goToAlbum"),
      icon: <Disc3 aria-hidden />,
      onSelect: () => void navigate({ to: "/album/$provider/$id", params: { provider: track.provider, id: track.album_provider_id! } }),
    },
    !!track.artist_provider_id && {
      type: "item",
      id: "artist",
      label: t("menu.goToArtist"),
      icon: <Mic2 aria-hidden />,
      onSelect: () => void navigate({ to: "/artist/$provider/$id", params: { provider: track.provider, id: track.artist_provider_id! } }),
    },
    !!onRemove && { type: "separator" },
    !!onRemove && { type: "item", id: "remove", label: t("menu.removeFromPlaylist"), icon: <X aria-hidden />, onSelect: onRemove, destructive: true },
  ];

  return (
    <>
      <ActionMenu entries={entries} label={t("menu.moreFor", { title: track.title })} heading={track.title} buttonClassName={buttonClassName}>
        {children}
      </ActionMenu>
      {canPlaylists && playlist.dialog}
    </>
  );
}
