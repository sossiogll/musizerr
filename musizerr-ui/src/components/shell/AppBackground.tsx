import { lazy, Suspense } from "react";
import { useQuery } from "@tanstack/react-query";
import { useIsSignedIn } from "@/contexts/PreferencesSync";
import { usePlayer } from "@/contexts/PlayerContext";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { getSettingsValues } from "@/lib/api-client";
import { BACKGROUND_DESKTOP_QUERY, BACKGROUND_SETTING_KEY, backgroundKind } from "@/lib/backgrounds";

// A separate chunk: nothing of the canvas code is fetched while the setting
// is off or the device is not a desktop.
const BackgroundCanvas = lazy(() => import("@/components/background/BackgroundCanvas"));

/**
 * The viewer's animated background (the personal `background` setting), or
 * nothing. Decided here, cheaply, before any of the canvas code exists:
 * the setting is off or absent, there is no account to own it (open mode), or
 * the device is not a desktop (BACKGROUND_DESKTOP_QUERY, followed live) →
 * null: no import, no canvas, no animation loop, no listeners but the one
 * media query.
 *
 * Shares the `settings-values` query with PreferencesSync, so it costs no
 * request of its own.
 */
export function AppBackground() {
  const signedIn = useIsSignedIn();
  const desktop = useMediaQuery(BACKGROUND_DESKTOP_QUERY);
  const { track, playing } = usePlayer();
  const { data: values } = useQuery({
    queryKey: ["settings-values"],
    queryFn: getSettingsValues,
    staleTime: 5 * 60 * 1000,
    enabled: signedIn,
  });

  const kind = signedIn && desktop ? backgroundKind(values?.[BACKGROUND_SETTING_KEY]) : null;
  if (!kind) return null;

  return (
    <Suspense fallback={null}>
      <BackgroundCanvas
        kind={kind}
        playing={playing}
        title={track?.title ?? null}
        artist={track?.artist ?? null}
      />
    </Suspense>
  );
}
