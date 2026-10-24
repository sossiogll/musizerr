import { useQueryClient } from "@tanstack/react-query";
import { RotateCw, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { checkNow, useConnectivity } from "@/lib/connectivity";
import { newestSavedAt } from "@/lib/query-persistence";
import { useCountdown } from "@/hooks/useCountdown";
import { useI18n } from "@/i18n";

/**
 * Shown while the API is unreachable: when the data on screen was saved,
 * when the next automatic attempt runs, and a way to retry now.
 */
export function OfflineBanner() {
  const { online, checking, nextCheckAt } = useConnectivity();
  const seconds = useCountdown(online ? null : nextCheckAt);
  const queryClient = useQueryClient();
  const { t, locale } = useI18n();
  if (online) return null;

  const savedAt = newestSavedAt(queryClient);
  const time = savedAt
    ? new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(savedAt)
    : null;

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-warning/30 bg-warning/10 px-4 py-2 text-sm"
    >
      <WifiOff aria-hidden className="size-4 shrink-0 text-warning" />
      <span className="font-medium">{t("connectivity.title")}</span>
      <span className="text-muted-foreground">
        {time ? t("connectivity.savedAt", { time }) : t("connectivity.noSavedData")}{" "}
        {checking
          ? t("connectivity.checking")
          : seconds !== null && t("connectivity.retryIn", { seconds })}
      </span>
      <Button
        variant="outline"
        size="sm"
        className="ml-auto h-8"
        onClick={() => void checkNow()}
        disabled={checking}
      >
        <RotateCw aria-hidden className={checking ? "animate-spin" : undefined} />
        {t("connectivity.retryNow")}
      </Button>
    </div>
  );
}
