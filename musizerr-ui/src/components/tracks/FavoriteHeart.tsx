import { memo } from "react";
import { Clock, Heart, Loader2 } from "lucide-react";
import type { HeartState } from "@/hooks/useTrackFavorite";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

// Fixed 44px: the heart never moves the status slot next to it.
const CELL = "grid size-11 shrink-0 place-items-center";

/**
 * The heart next to the status slot. Only for a track in the library
 * (filled = favorite); a small empty heart with a clock while a download of
 * the viewer's will add it on arrival; nothing otherwise (no disabled
 * hearts).
 */
export const FavoriteHeart = memo(function FavoriteHeart({ state, title }: { state: HeartState; title: string }) {
  const { t } = useI18n();

  // Keeps the column; never catches a click meant for the slot next to it.
  if (state.kind === "hidden") return <span className={cn(CELL, "pointer-events-none")} aria-hidden />;

  if (state.kind === "pending") {
    const label = t("heart.pending");
    return (
      <span className={CELL} role="img" aria-label={label} title={label}>
        <span className="relative text-muted-foreground">
          <Heart aria-hidden className="size-4" />
          <Clock
            aria-hidden
            className="absolute -bottom-1 -right-1.5 size-3 rounded-full bg-background text-primary motion-safe:animate-pulse"
          />
        </span>
      </span>
    );
  }

  const label = t(state.favorite ? "heart.remove" : "heart.add", { title });
  return (
    <button
      type="button"
      aria-pressed={state.favorite}
      aria-label={label}
      title={label}
      disabled={state.busy}
      onClick={(e) => {
        e.stopPropagation();
        state.toggle();
      }}
      className={cn(
        CELL,
        "rounded-md transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60",
        state.favorite ? "text-primary" : "text-muted-foreground hover:text-foreground",
      )}
    >
      {state.busy ? (
        <Loader2 aria-hidden className="size-4 animate-spin" />
      ) : (
        <Heart
          aria-hidden
          className="size-4 motion-safe:transition-transform motion-safe:active:scale-90"
          fill={state.favorite ? "currentColor" : "none"}
        />
      )}
    </button>
  );
});
