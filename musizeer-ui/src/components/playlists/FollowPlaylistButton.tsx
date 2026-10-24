import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { BookmarkCheck, BookmarkPlus, ChevronDown, Loader2 } from "lucide-react";
import { useApproval } from "@/hooks/useDownloadApproval";
import { useFollowPlaylist } from "@/hooks/useFollowPlaylist";
import { useServerAction } from "@/hooks/useServerAction";
import { PLAYLIST_TRACKS_RESTRICTED } from "@/hooks/useUserPlaylists";
import { usePublicProviders } from "@/hooks/usePublicProviders";
import { useI18n } from "@/i18n";
import { DeletePlaylistDialog } from "@/components/playlists/DeletePlaylistDialog";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";

/**
 * "Follow" on the page of a playlist of a metadata provider: it keeps the
 * playlist among the viewer's own (read-only, followed as its source changes)
 * and asks to download its tracks, through the approval policy; they do not
 * become favorites. Once followed it is a menu: open it in the viewer's
 * playlists, or unfollow it (asking whether to also delete the tracks that were
 * downloaded for it). Hidden for a viewer without playlists.
 */
export function FollowPlaylistButton({ provider, id, name, tracks }: { provider: string; id: string; name: string; tracks: number | null }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const server = useServerAction();
  const approval = useApproval("playlist", tracks);
  const { can: providerCan } = usePublicProviders();
  const { can, followed, follow, unfollow } = useFollowPlaylist({ provider, id, name });
  const [unfollowing, setUnfollowing] = useState(false);

  if (!can) return null;
  const restricted = providerCan(provider, PLAYLIST_TRACKS_RESTRICTED);
  const hint = [t("playlists.followHint"), restricted ? t("playlists.restrictedHint") : undefined, approval.waits ? approval.hint : undefined]
    .filter(Boolean)
    .join(" ");

  if (!followed) {
    return (
      <Button size="lg" variant="outline" className="px-5" onClick={() => follow.mutate()} disabled={follow.isPending || server.disabled} title={server.reason ?? hint}>
        {follow.isPending ? <Loader2 aria-hidden className="animate-spin" /> : <BookmarkPlus aria-hidden />}
        {t("playlists.follow")}
      </Button>
    );
  }
  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="lg" variant="outline" className="px-5" disabled={unfollow.isPending} aria-label={`${t("playlists.following")}: ${name}`}>
            {unfollow.isPending ? <Loader2 aria-hidden className="animate-spin" /> : <BookmarkCheck aria-hidden className="text-primary" />}
            {t("playlists.following")}
            <ChevronDown aria-hidden />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem onSelect={() => void navigate({ to: "/playlists/$id", params: { id: followed.id } })}>
            {t("playlists.openMine")}
          </DropdownMenuItem>
          <DropdownMenuItem className="text-destructive focus:text-destructive" disabled={server.disabled} onSelect={() => setUnfollowing(true)}>
            {t("playlists.unfollow")}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <DeletePlaylistDialog playlist={followed} open={unfollowing} onOpenChange={setUnfollowing} onConfirm={(deleteTracks) => unfollow.mutate(deleteTracks)} />
    </>
  );
}
