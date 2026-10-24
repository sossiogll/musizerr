import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ListMusic, Plus } from "lucide-react";
import { toast } from "sonner";
import { createUserPlaylist } from "@/lib/api-client";
import { useServerAction } from "@/hooks/useServerAction";
import { usePlaylistFailure, useRefreshPlaylists, useUserPlaylists } from "@/hooks/useUserPlaylists";
import { usePublicProviders } from "@/hooks/usePublicProviders";
import { useI18n } from "@/i18n";
import { ApiErrorMessage } from "@/components/ErrorMessage";
import { CoverImage } from "@/components/CoverImage";
import { EmptyState } from "@/components/ContentList";
import { UserPlaylistMenu } from "@/components/menu/UserPlaylistMenu";
import { PlaylistNameDialog } from "@/components/playlists/PlaylistNameDialog";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { UserPlaylist } from "@/types";

/** The viewer's playlists: their own and the ones they follow. Private to them. */
export function PlaylistsPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const server = useServerAction();
  const failure = usePlaylistFailure();
  const refresh = useRefreshPlaylists();
  const { data, isLoading, error } = useUserPlaylists();
  const [naming, setNaming] = useState(false);

  const create = useMutation({
    mutationFn: (name: string) => createUserPlaylist(name),
    onSuccess: (playlist) => {
      refresh();
      toast.success(t("playlists.created", { name: playlist.name }));
      void navigate({ to: "/playlists/$id", params: { id: playlist.id } });
    },
    onError: (e) => failure(e, t("playlists.createFailed")),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="font-display text-2xl font-bold">{t("playlists.title")}</h1>
        <Button onClick={() => setNaming(true)} disabled={server.disabled || create.isPending} title={server.reason}>
          <Plus aria-hidden />
          {t("playlists.new")}
        </Button>
      </div>

      {isLoading ? (
        <div aria-hidden className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : !data ? (
        <ApiErrorMessage className="py-12 text-center text-sm text-muted-foreground" error={error} fallback={t("playlists.loadFailed")} />
      ) : data.items.length === 0 ? (
        <EmptyState icon={ListMusic} message={t("playlists.empty")} />
      ) : (
        <ul aria-label={t("playlists.title")} className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {data.items.map((playlist) => (
            <li key={playlist.id}>
              <PlaylistCard playlist={playlist} />
            </li>
          ))}
        </ul>
      )}

      <PlaylistNameDialog open={naming} onOpenChange={setNaming} title={t("playlists.newTitle")} confirmLabel={t("playlists.create")} onConfirm={(name) => create.mutate(name)} />
    </div>
  );
}

function PlaylistCard({ playlist }: { playlist: UserPlaylist }) {
  const { t } = useI18n();
  const { label } = usePublicProviders();
  return (
    <UserPlaylistMenu playlist={playlist} openable buttonClassName="absolute right-1 top-1/2 -translate-y-1/2">
      {({ surface, button }) => (
        <div {...surface} className={cn("relative", surface.className)}>
          <Link
            to="/playlists/$id"
            params={{ id: playlist.id }}
            className="flex min-h-24 items-center gap-3 rounded-lg border p-3 pr-14 transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <CoverImage src={playlist.cover_url} alt="" type="playlist" className="size-16 rounded-md" />
            <div className="min-w-0 flex-1 space-y-1">
              <p className="truncate font-medium">{playlist.name}</p>
              <p className="truncate text-sm text-muted-foreground">
                {t("common.trackCount", { count: playlist.track_count })}
                {playlist.kind === "followed" && playlist.source_provider && ` · ${t("playlists.followedFrom", { provider: label(playlist.source_provider) })}`}
              </p>
              {playlist.kind === "followed" && <Badge variant="secondary">{t("playlists.kindFollowed")}</Badge>}
            </div>
          </Link>
          {button}
        </div>
      )}
    </UserPlaylistMenu>
  );
}
