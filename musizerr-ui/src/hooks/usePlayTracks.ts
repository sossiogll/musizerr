import { useCallback, useState } from "react";
import { toast } from "sonner";
import { isPlayable, toPlayerTrack, usePlayer } from "@/contexts/PlayerContext";
import { useI18n } from "@/i18n";
import type { Track } from "@/types";

/**
 * Plays, or queues, the tracks of a collection the viewer has not opened:
 * `load` reads them when asked (an album, a playlist, an artist's popular
 * tracks), so a tile or a menu costs nothing until it is used. Only what the
 * server says can play is queued; with nothing to play it says so.
 */
export function usePlayTracks() {
  const { t } = useI18n();
  const { play, addToQueue } = usePlayer();
  const [busy, setBusy] = useState(false);

  const run = useCallback(
    async (load: () => Promise<Track[]>, use: (queue: Track[]) => void, title: string) => {
      setBusy(true);
      try {
        const playable = (await load()).filter(isPlayable);
        if (playable.length === 0) toast.info(t("menu.nothingToPlay", { title }));
        else use(playable);
      } catch {
        toast.error(t("menu.tracksFailed", { title }));
      } finally {
        setBusy(false);
      }
    },
    [t],
  );

  const playAll = useCallback(
    (load: () => Promise<Track[]>, title: string) =>
      run(
        load,
        (tracks) => {
          const queue = tracks.map(toPlayerTrack);
          play(queue[0], queue);
        },
        title,
      ),
    [run, play],
  );

  const queueAll = useCallback(
    (load: () => Promise<Track[]>, title: string) =>
      run(
        load,
        (tracks) => {
          tracks.forEach((track) => addToQueue(toPlayerTrack(track)));
          toast.success(t("menu.addedAllToQueue", { count: tracks.length, title }));
        },
        title,
      ),
    [run, addToQueue, t],
  );

  return { playAll, queueAll, busy };
}
