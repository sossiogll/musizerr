import { AlertTriangle } from "lucide-react";
import { useI18n } from "@/i18n";
import type { PermissionsDiff } from "@/types";
import { usePermissionLines } from "./PermissionsList";

/**
 * What an update changes in what the admin approved: what it adds, what it drops, the limits that move, the
 * roles it gains or loses. An update that changes nothing says so.
 */
export function PermissionsDiffView({ diff }: { diff: PermissionsDiff }) {
  const { t } = useI18n();
  const added = usePermissionLines({
    egress: diff.egress.added,
    egress_from_settings: diff.egress_from_settings.added,
    filesystem_from_settings: diff.filesystem_from_settings.added,
    tcp_from_settings: diff.tcp_from_settings.added,
    limits: {},
  });
  const removed = usePermissionLines({
    egress: diff.egress.removed,
    egress_from_settings: diff.egress_from_settings.removed,
    filesystem_from_settings: diff.filesystem_from_settings.removed,
    tcp_from_settings: diff.tcp_from_settings.removed,
    limits: {},
  });
  const limit = (value: number | null) => (value === null ? t("sources.updateDialog.defaultLimit") : String(value));
  const limits = diff.limits.map((change) =>
    t("sources.updateDialog.limit", { name: change.name.replace(/_/g, " "), old: limit(change.old), new: limit(change.new) }),
  );

  if (diff.is_empty) return <p className="text-sm text-muted-foreground">{t("sources.updateDialog.same")}</p>;
  return (
    <div className="space-y-3">
      {diff.grants_more && (
        <p role="alert" className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/10 p-2.5 text-sm">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning" aria-hidden />
          {t("sources.updateDialog.grantsMore")}
        </p>
      )}
      <DiffGroup title={t("sources.updateDialog.added")} sign="+" lines={[...added, ...limits]} />
      <DiffGroup title={t("sources.updateDialog.removed")} sign="−" lines={removed} />
      {diff.roles.added.length > 0 && <p className="text-sm">{t("sources.updateDialog.rolesAdded", { roles: diff.roles.added.join(", ") })}</p>}
      {diff.roles.removed.length > 0 && <p className="text-sm">{t("sources.updateDialog.rolesRemoved", { roles: diff.roles.removed.join(", ") })}</p>}
    </div>
  );
}

function DiffGroup({ title, sign, lines }: { title: string; sign: string; lines: string[] }) {
  if (lines.length === 0) return null;
  return (
    <section aria-label={title} className="space-y-1">
      <h3 className="text-sm font-medium">{title}</h3>
      <ul className="space-y-0.5 text-sm">
        {lines.map((line) => (
          <li key={line} className="flex gap-2">
            <span aria-hidden className="w-3 shrink-0 font-mono">
              {sign}
            </span>
            <span>{line}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
