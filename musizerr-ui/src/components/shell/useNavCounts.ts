import { useActiveDownloadCount } from "@/hooks/useDownloadsSummary";
import { usePendingRequestCount } from "@/hooks/usePendingRequestCount";
import { useI18n, type MessageKey } from "@/i18n";
import type { NavItem } from "./nav-items";

type NavBadge = NonNullable<NavItem["badge"]>;

const LABEL: Record<NavBadge, MessageKey> = {
  downloads: "shell.downloadsBadge",
  requests: "shell.requestsBadge",
};

/** What a navigation item counts, and how to say it: the number of its badge (0 shows none). */
export function useNavCounts() {
  const { t } = useI18n();
  const counts: Record<NavBadge, number> = {
    downloads: useActiveDownloadCount(),
    requests: usePendingRequestCount(),
  };
  return (item: NavItem): { count: number; label: string } => {
    if (!item.badge) return { count: 0, label: "" };
    return { count: counts[item.badge], label: t(LABEL[item.badge], { count: counts[item.badge] }) };
  };
}
