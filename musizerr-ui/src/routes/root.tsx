import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Outlet } from "@tanstack/react-router";
import { QueueProvider } from "@/contexts/QueueContext";
import { PlayerProvider, toPlayerTrack, type PlayerTrack } from "@/contexts/PlayerContext";
import { refetchTrack } from "@/lib/fresh-track";
import { AuthProvider } from "@/contexts/AuthContext";
import { PreferencesSync } from "@/contexts/PreferencesSync";
import { AppGate } from "@/components/connectivity/AppGate";
import { AppShell } from "@/components/shell/AppShell";

export function RootLayout() {
  const queryClient = useQueryClient();
  // A source the player could not play may have expired with the list that
  // brought it: the lists on screen are asked again before its one retry.
  const refreshTrack = useCallback(
    async (track: PlayerTrack) => {
      const fresh = await refetchTrack(queryClient, track);
      return fresh ? toPlayerTrack(fresh) : null;
    },
    [queryClient],
  );
  return (
    <AuthProvider>
      <AppGate>
        <PreferencesSync />
        <QueueProvider>
          <PlayerProvider refreshTrack={refreshTrack}>
            <AppShell>
              <Outlet />
            </AppShell>
          </PlayerProvider>
        </QueueProvider>
      </AppGate>
    </AuthProvider>
  );
}
