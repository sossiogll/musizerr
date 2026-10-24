import { memo, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { Hourglass } from "lucide-react";
import { CoverImage } from "@/components/CoverImage";
import { CollectionMenu, type CollectionItem } from "@/components/menu/CollectionMenu";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

function describe(item: CollectionItem) {
  switch (item.kind) {
    case "album":
      return { title: item.title, subtitle: [item.artist, item.year].filter(Boolean).join(" · "), to: "/album/$provider/$id" as const };
    case "artist":
      return { title: item.name, subtitle: null, to: "/artist/$provider/$id" as const };
    case "playlist":
      return { title: item.name, subtitle: null, to: "/playlist/$provider/$id" as const };
  }
}

interface MediaTileProps {
  item: CollectionItem;
  /** Extra line under the title (e.g. editions, library coverage). */
  meta?: ReactNode;
  /** A badge over the cover (e.g. "2 in library"). */
  badge?: ReactNode;
  /** Offer "download all" in its menu (hidden anyway without the download capability). */
  downloadable?: boolean;
}

/**
 * A cover tile for an album, an artist or a playlist, chosen by the item's
 * `kind` — never guessed from its fields. Everything else it can do (play,
 * queue, download, follow, go to) is in its menu: the "⋯" button, a right
 * click or a long press.
 */
export const MediaTile = memo(function MediaTile({ item, meta, badge, downloadable = false }: MediaTileProps) {
  const { t } = useI18n();
  const { title, subtitle, to } = describe(item);
  const subtitleText = subtitle || (item.kind === "playlist" ? t("tile.playlist") : item.kind === "artist" ? t("tile.artist") : "");

  return (
    <CollectionMenu item={item} downloadable={downloadable} buttonClassName="absolute bottom-0 right-0">
      {({ surface, button, waiting }) => (
        <article {...surface} className={cn("group relative min-w-0", surface.className)}>
          <Link
            to={to}
            params={{ provider: item.provider, id: item.provider_id }}
            className="block space-y-2 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <div className="relative">
              <CoverImage
                src={item.cover_url}
                alt=""
                type={item.kind}
                size="lg"
                className={cn("aspect-square h-auto w-full transition-transform group-hover:scale-[1.02]", item.kind !== "artist" && "rounded-lg")}
              />
              {badge && <span className="absolute left-2 top-2">{badge}</span>}
            </div>
            {/* Room for the button next to the text (both sides for the centered artist). */}
            <div className={cn("min-w-0 pr-10", item.kind === "artist" && "px-10 text-center")}>
              <p className="truncate text-sm font-semibold">{title}</p>
              {subtitleText && <p className="truncate text-xs text-muted-foreground">{subtitleText}</p>}
              {meta && <div className="mt-1 text-xs text-muted-foreground">{meta}</div>}
            </div>
          </Link>
          {waiting && (
            <span
              role="status"
              title={t("approval.waiting")}
              aria-label={t("approval.waitingItem", { title })}
              className="absolute bottom-0 right-11 grid size-11 place-items-center text-warning"
            >
              <Hourglass aria-hidden className="size-4" />
            </span>
          )}
          {button}
        </article>
      )}
    </CollectionMenu>
  );
});

/** The responsive grid tiles sit in: 2 columns on a phone up to 6 on XL. */
export const TILE_GRID = "grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6";
