import { Outlet } from "@tanstack/react-router";
import { CountBadge } from "@/components/shell/CountBadge";
import { TabLink, TabNav } from "@/components/TabNav";
import { usePendingRequestCount } from "@/hooks/usePendingRequestCount";
import { useI18n, type MessageKey } from "@/i18n";

const SECTIONS = [
  { to: "/admin/users", labelKey: "adminArea.users" },
  { to: "/admin/requests", labelKey: "adminArea.requests", counts: "requests" },
  { to: "/admin/providers", labelKey: "adminArea.providers" },
  { to: "/admin/routines", labelKey: "adminArea.routines" },
  { to: "/admin/sources", labelKey: "adminArea.sources" },
  { to: "/admin/duplicates", labelKey: "adminArea.duplicates" },
  { to: "/admin/system", labelKey: "adminArea.system" },
] as const satisfies readonly { to: string; labelKey: MessageKey; counts?: "requests" }[];

/** Administration: one place for users, requests, providers, routines, sources, duplicates and system settings. */
export function AdminLayout() {
  const { t } = useI18n();
  const waiting = usePendingRequestCount();
  return (
    <div className="space-y-6">
      <h1 className="font-display text-2xl font-bold max-sm:sr-only">{t("adminArea.title")}</h1>
      <TabNav label={t("adminArea.sections")}>
        {SECTIONS.map((section) => (
          <TabLink key={section.to} to={section.to}>
            {t(section.labelKey)}
            {"counts" in section && waiting > 0 && <CountBadge count={waiting} label={t("shell.requestsBadge", { count: waiting })} />}
          </TabLink>
        ))}
      </TabNav>
      <Outlet />
    </div>
  );
}
