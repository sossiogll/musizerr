import { useConnectivity } from "@/lib/connectivity";
import { useI18n } from "@/i18n";
import { cn } from "@/lib/utils";

/** Is the server answering: a dot, and its meaning in words (never color alone). */
export function ServiceStatus({ withText = false, className }: { withText?: boolean; className?: string }) {
  const { online } = useConnectivity();
  const { t } = useI18n();
  const text = online ? t("shell.servicesOk") : t("shell.servicesDown");
  return (
    <span className={cn("inline-flex items-center gap-2 text-xs text-muted-foreground", className)} title={text}>
      <span aria-hidden className={cn("size-2 shrink-0 rounded-full", online ? "bg-success" : "bg-warning")} />
      <span className={withText ? "truncate" : "sr-only"}>{text}</span>
    </span>
  );
}
