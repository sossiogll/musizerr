import { Loader2, WifiOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { checkNow, useConnectivity } from "@/lib/connectivity";
import { useCountdown } from "@/hooks/useCountdown";
import { useI18n } from "@/i18n";

/** The API has never answered on this device and nothing is saved: nothing to show yet. */
export function FirstLoadScreen() {
  const { checking, nextCheckAt } = useConnectivity();
  const seconds = useCountdown(nextCheckAt);
  const { t } = useI18n();

  return (
    <main className="mx-auto flex min-h-dvh max-w-md flex-col justify-center gap-6 px-6">
      <span className="grid size-14 place-items-center rounded-lg bg-warning/15 text-warning">
        <WifiOff aria-hidden className="size-7" />
      </span>
      <div className="space-y-2">
        <h1 className="font-display text-3xl font-bold leading-tight">{t("firstLoad.title")}</h1>
        <p className="text-muted-foreground">{t("firstLoad.body")}</p>
      </div>
      <section className="space-y-2 rounded-lg border bg-card p-4">
        <h2 className="text-sm font-semibold">{t("firstLoad.adminTitle")}</h2>
        <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          <li>{t("firstLoad.tipContainer")}</li>
          <li>{t("firstLoad.tipLogs")}</li>
          <li>{t("firstLoad.tipProxy")}</li>
        </ul>
      </section>
      <div className="flex flex-col items-center gap-2">
        <Button variant="brand" size="lg" className="w-full" onClick={() => void checkNow()} disabled={checking}>
          {checking && <Loader2 aria-hidden className="animate-spin" />}
          {t("connectivity.retryNow")}
        </Button>
        <p className="text-sm text-muted-foreground" aria-live="polite">
          {checking ? t("connectivity.checking") : seconds !== null && t("connectivity.retryIn", { seconds })}
        </p>
      </div>
    </main>
  );
}

export function LoadingScreen() {
  const { t } = useI18n();
  return (
    <main className="grid min-h-dvh place-items-center" aria-busy="true">
      <span className="flex items-center gap-2 text-muted-foreground">
        <Loader2 aria-hidden className="size-5 animate-spin" />
        {t("firstLoad.loading")}
      </span>
    </main>
  );
}
