import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Pagination types
// ---------------------------------------------------------------------------

interface ServerPagination {
  mode: "pages";
  page: number;
  pages: number;
  onPageChange: (page: number) => void;
}

interface LoadMorePagination {
  mode: "load-more";
  visibleCount: number;
  totalCount: number;
  onLoadMore: () => void;
}

type Pagination = ServerPagination | LoadMorePagination;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ContentListProps<T> {
  items: T[];
  renderItem: (item: T, index: number) => ReactNode;
  keyExtractor: (item: T, index: number) => string;

  pagination?: Pagination;

  loading?: boolean;
  skeletonCount?: number;
  skeletonHeight?: string;

  empty?: {
    icon?: LucideIcon;
    message: string;
  };

  layout?: "list" | "grid";
  gap?: string;
  className?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ContentList<T>({
  items,
  renderItem,
  keyExtractor,
  pagination,
  loading = false,
  skeletonCount = 8,
  skeletonHeight = "h-16",
  empty,
  layout = "list",
  gap,
  className,
}: ContentListProps<T>) {
  if (loading) {
    return (
      <div
        className={cn(
          layout === "grid"
            ? "grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4"
            : "space-y-2",
          gap ?? (layout === "grid" ? "gap-3" : undefined),
          className,
        )}
      >
        {Array.from({ length: skeletonCount }).map((_, i) => (
          <Skeleton
            key={i}
            className={cn(
              skeletonHeight,
              layout === "grid" && "aspect-square h-auto",
            )}
          />
        ))}
      </div>
    );
  }

  if (items.length === 0 && empty) {
    return <EmptyState icon={empty.icon} message={empty.message} />;
  }

  const visibleItems =
    pagination?.mode === "load-more"
      ? items.slice(0, pagination.visibleCount)
      : items;

  return (
    <>
      <div
        className={cn(
          layout === "grid"
            ? "grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4"
            : "space-y-2",
          gap ?? (layout === "grid" ? "gap-3" : undefined),
          className,
        )}
      >
        {visibleItems.map((item, index) => (
          <div key={keyExtractor(item, index)}>{renderItem(item, index)}</div>
        ))}
      </div>

      {pagination?.mode === "load-more" && (
        <LoadMoreButton visible={pagination.visibleCount} total={pagination.totalCount} onLoadMore={pagination.onLoadMore} />
      )}

      {pagination?.mode === "pages" && (
        <PageNav page={pagination.page} pages={pagination.pages} onPageChange={pagination.onPageChange} />
      )}
    </>
  );
}

/** "Load more (N remaining)", shown while some items are hidden. */
export function LoadMoreButton({ visible, total, onLoadMore }: { visible: number; total: number; onLoadMore: () => void }) {
  const { t } = useI18n();
  if (visible >= total) return null;
  return (
    <Button variant="outline" onClick={onLoadMore} className="mt-3 h-11 w-full sm:h-9">
      {t("common.loadMore", { count: total - visible })}
    </Button>
  );
}

/** Previous / page n of m / Next, for server-paginated lists; nothing with one page. */
export function PageNav({ page, pages, onPageChange }: { page: number; pages: number; onPageChange: (page: number) => void }) {
  const { t } = useI18n();
  if (pages <= 1) return null;
  return (
    <nav aria-label={t("common.pagination")} className="flex items-center justify-center gap-2 pt-2">
      <Button variant="outline" size="sm" disabled={page === 1} onClick={() => onPageChange(page - 1)}>
        {t("common.previous")}
      </Button>
      <span className="px-3 py-1 text-sm text-muted-foreground tabular-nums">
        {page} / {pages}
      </span>
      <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => onPageChange(page + 1)}>
        {t("common.next")}
      </Button>
    </nav>
  );
}

/** A list with nothing in it: an icon and one line. */
export function EmptyState({ icon: Icon, message, children }: { icon?: LucideIcon; message: string; children?: ReactNode }) {
  return (
    <div className="space-y-3 py-12 text-center">
      {Icon && <Icon aria-hidden className="mx-auto size-10 text-muted-foreground/40" />}
      <p className="text-sm text-muted-foreground">{message}</p>
      {children}
    </div>
  );
}
