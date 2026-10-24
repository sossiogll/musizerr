import { AlertTriangle, ArrowUpCircle } from "lucide-react";
import { useUpdates } from "@/hooks/useUpdates";
import { useI18n, type MessageKey } from "@/i18n";
import type { UpdateReason } from "@/lib/api-client";
import { cn } from "@/lib/utils";

const REASON_KEYS: Record<UpdateReason["code"], MessageKey> = {
  major: "updates.reasons.major",
  upgrade_path: "updates.reasons.upgrade_path",
  contract_unsupported: "updates.reasons.contract_unsupported",
  contract_migrations: "updates.reasons.contract_migrations",
  manual_steps: "updates.reasons.manual_steps",
  no_metadata: "updates.reasons.no_metadata",
};

/**
 * "Musizeer X is available" at the top of Administration (ADR-0007 §5.1): the newest release newer than
 * the one that runs, how to update to it by hand and, when pulling its images is not all it takes, why.
 * Musizeer never updates itself. Renders nothing when there is no newer release.
 */
export function UpdateNotice({ className }: { className?: string }) {
  const { t, locale } = useI18n();
  const { data } = useUpdates();
  const newest = data?.updates[0];
  if (!data || !newest) return null;

  const list = (items: string[]) => new Intl.ListFormat(locale, { type: "conjunction" }).format(items);
  const others = data.updates.length - 1;
  return (
    <section
      role="status"
      aria-label={t("updates.title", { version: newest.version })}
      className={cn(
        "flex items-start gap-3 rounded-md border px-4 py-3 text-sm",
        newest.plain ? "border-info/30 bg-info/10" : "border-warning/30 bg-warning/10",
        className,
      )}
    >
      {newest.plain ? (
        <ArrowUpCircle aria-hidden className="mt-0.5 size-5 shrink-0 text-info" />
      ) : (
        <AlertTriangle aria-hidden className="mt-0.5 size-5 shrink-0 text-warning" />
      )}
      <div className="min-w-0 space-y-1.5">
        <p>
          <span className="font-medium">{t("updates.title", { version: newest.version })}</span>{" "}
          <span className="text-muted-foreground">
            {t(`updates.kind.${newest.kind}`)} · {t("updates.running", { version: data.current })}
            {others > 0 && <> · {t("updates.others", { count: others })}</>}
          </span>
        </p>
        {newest.plain ? (
          <p className="text-muted-foreground">{t("updates.plain")}</p>
        ) : (
          <ul className="list-disc space-y-0.5 pl-5 text-muted-foreground">
            {newest.reasons.map((reason) => (
              <li key={reason.code}>
                {t(REASON_KEYS[reason.code], { through: reason.through ?? "", providers: list(reason.providers ?? []) })}
              </li>
            ))}
          </ul>
        )}
        <p className="text-muted-foreground">
          {t("updates.howTo", { version: newest.version })}
          {newest.url && (
            <>
              {" "}
              <a href={newest.url} target="_blank" rel="noreferrer" className="font-medium text-primary underline-offset-4 hover:underline">
                {t("updates.notes")}
              </a>
            </>
          )}
        </p>
      </div>
    </section>
  );
}
