import { useI18n } from "@/i18n";
import type { ProviderPermissions } from "@/types";

/** The lines that say what a sandbox is granted, in words: hosts, settings-named hosts, raw-TCP hosts, folders, limits. */
export function usePermissionLines(permissions: ProviderPermissions): string[] {
  const { t } = useI18n();
  const limits = permissions.limits ?? {};
  return [
    ...permissions.egress.map((host) => egressLine(t, host)),
    ...permissions.egress_from_settings.map((setting) => t("sources.permissions.fromSetting", { setting })),
    ...permissions.tcp_from_settings.map((access) =>
      access.ports === "any"
        ? t("sources.permissions.tcpAny", { setting: access.setting })
        : t("sources.permissions.tcpPorts", { setting: access.setting, ports: access.ports }),
    ),
    ...permissions.filesystem_from_settings.map((access) =>
      t(access.mode === "rw" ? "sources.permissions.folderRw" : "sources.permissions.folderRo", { setting: access.setting }),
    ),
    ...(limits.memory_mb != null ? [t("sources.permissions.memory", { value: limits.memory_mb })] : []),
    ...(limits.open_files != null ? [t("sources.permissions.files", { value: limits.open_files })] : []),
    ...(limits.processes != null ? [t("sources.permissions.processes", { value: limits.processes })] : []),
  ];
}

/** What a provider will be allowed. Nothing declared is said as such: no network, no folder, default limits. */
export function PermissionsList({ permissions }: { permissions: ProviderPermissions }) {
  const { t } = useI18n();
  const lines = usePermissionLines(permissions);
  return (
    <section aria-label={t("sources.permissions.heading")} className="space-y-1.5">
      <h3 className="text-sm font-medium">{t("sources.permissions.heading")}</h3>
      {lines.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("sources.permissions.none")}</p>
      ) : (
        <ul className="list-disc space-y-0.5 pl-5 text-sm">
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** A fixed destination in words: a wildcard (`*.example.com:443`) is any subdomain, never the domain itself. */
function egressLine(t: ReturnType<typeof useI18n>["t"], host: string): string {
  if (!host.startsWith("*.")) return t("sources.permissions.egress", { host });
  const separator = host.lastIndexOf(":");
  return t("sources.permissions.egressWildcard", { domain: host.slice(2, separator), port: host.slice(separator + 1) });
}
