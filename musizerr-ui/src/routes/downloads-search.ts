/**
 * The Downloads page's search params. Apart from the page so the router can
 * validate the URL without loading the page's code (routes are code-split).
 */
export const DOWNLOADS_TABS = ["all", "active", "queued", "review", "done", "requests"] as const;

export type DownloadsTab = (typeof DOWNLOADS_TABS)[number];

/** The URL's `?tab=`: an unknown value falls back to "all". */
export function parseDownloadsSearch(search: Record<string, unknown>): { tab: DownloadsTab } {
  const tab = DOWNLOADS_TABS.find((t) => t === search.tab);
  return { tab: tab ?? "all" };
}
