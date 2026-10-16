import { useState } from "react";
import { CoverageShort } from "@/components/LibraryCoverage";
import { MediaTile } from "@/components/MediaTile";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";
import type { Album, Artist, Playlist } from "@/types";

type ShelfItem = Album | Artist | Playlist;

/** Tiles shown on M and up before "Show all": 3 rows on M, 2 on L/XL. */
const COLLAPSED = 12;

// Below M a shelf is one row scrolled sideways; from M it is a grid.
const LAYOUT =
  "flex snap-x gap-4 overflow-x-auto pb-2 [scrollbar-width:thin] md:grid md:grid-cols-4 md:overflow-visible md:pb-0 lg:grid-cols-6";
const ITEM = "w-36 shrink-0 snap-start sm:w-40 md:w-auto";

export const tileKey = (item: ShelfItem) => `${item.kind}:${item.provider}:${item.provider_id}`;

/**
 * A row of cover tiles (albums, artists, playlists): swiped sideways on a
 * phone, a grid of up to two rows on wider screens with "Show all" for the
 * rest.
 */
export function TileShelf({
  items,
  loading = false,
  label,
  downloadable = false,
}: {
  items: ShelfItem[] | undefined;
  loading?: boolean;
  /** The accessible name of the list (usually the section title). */
  label: string;
  downloadable?: boolean;
}) {
  const { t } = useI18n();
  const [expanded, setExpanded] = useState(false);

  if (loading) {
    return (
      <div aria-hidden className={cn(LAYOUT, "overflow-hidden")}>
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className={cn(ITEM, "space-y-2")}>
            <Skeleton className="aspect-square w-full rounded-lg" />
            <Skeleton className="h-4 w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </div>
        ))}
      </div>
    );
  }
  if (!items?.length) return null;

  const hidden = items.length - COLLAPSED;
  return (
    <div>
      <ul aria-label={label} className={LAYOUT}>
        {items.map((item, i) => (
          <li key={tileKey(item)} className={cn(ITEM, !expanded && i >= COLLAPSED && "md:hidden")}>
            <MediaTile
              item={item}
              downloadable={downloadable}
              meta={item.kind === "album" ? <CoverageShort coverage={item.library} /> : undefined}
            />
          </li>
        ))}
      </ul>
      {hidden > 0 && !expanded && (
        <Button variant="ghost" size="sm" className="mt-2 hidden md:inline-flex" onClick={() => setExpanded(true)}>
          {t("common.showAll", { count: items.length })}
        </Button>
      )}
    </div>
  );
}
