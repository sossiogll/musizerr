import { Download, Heart, Home, Info, ListMusic, Search, Settings, Shield, type LucideIcon } from "lucide-react";
import type { Capability } from "@/hooks/useCapabilities";
import type { MessageKey } from "@/i18n";

export interface NavItem {
  to: string;
  labelKey: MessageKey;
  /** Shorter label for the top bar, where space is tight. */
  shortLabelKey?: MessageKey;
  icon: LucideIcon;
  /** Shown only with this capability (any of them, given a list). */
  capability?: Capability | readonly Capability[];
  /** Active for every path under this prefix; exact match otherwise. */
  matchPrefix?: string;
  /** A counter shown next to the item: the downloads in progress, the requests waiting for an admin. */
  badge?: "downloads" | "requests";
  /** The shell's search field stands in for this item in the top bar
   *  (L/M/S), for viewers who can search. */
  searchField?: true;
}

/**
 * The navigation, defined once. The shell renders it as a sidebar (XL), a
 * top bar (L, M, S) or a bottom bar (XS): the primary items go everywhere,
 * the secondary ones in the sidebar/top bar and, on XS, in the account menu.
 */
export const PRIMARY_NAV: readonly NavItem[] = [
  { to: "/", labelKey: "nav.home", icon: Home },
  { to: "/search", labelKey: "nav.search", icon: Search, matchPrefix: "/search", searchField: true },
  { to: "/favorites", labelKey: "nav.favorites", icon: Heart, capability: "favorite", matchPrefix: "/favorites" },
  { to: "/playlists", labelKey: "nav.playlists", icon: ListMusic, capability: "playlists", matchPrefix: "/playlists" },
  { to: "/downloads", labelKey: "nav.downloads", icon: Download, capability: "download", matchPrefix: "/downloads", badge: "downloads" },
];

export const SECONDARY_NAV: readonly NavItem[] = [
  { to: "/admin", labelKey: "nav.administration", shortLabelKey: "nav.admin", icon: Shield, capability: "admin", matchPrefix: "/admin", badge: "requests" },
  { to: "/config", labelKey: "nav.settings", icon: Settings, capability: ["settings_personal", "admin"], matchPrefix: "/config" },
  // For every viewer: the attribution and the legal notices must be reachable from the navigation (NOTICE.md).
  { to: "/about", labelKey: "nav.about", icon: Info },
];

export function isActive(item: NavItem, pathname: string): boolean {
  return item.matchPrefix ? pathname === item.matchPrefix || pathname.startsWith(`${item.matchPrefix}/`) : pathname === item.to;
}

export function allowed(item: NavItem, can: Record<Capability, boolean>): boolean {
  if (!item.capability) return true;
  const wanted: readonly Capability[] = typeof item.capability === "string" ? [item.capability] : item.capability;
  return wanted.some((c) => can[c]);
}
