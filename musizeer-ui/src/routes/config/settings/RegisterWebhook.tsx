import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n";
import { registerDelegationWebhooks, type DelegationWebhookRegistrationResult } from "@/lib/api-client";
import { useErrorText } from "@/lib/error-text";

/**
 * One provider's registration in words: what the external system accepted, that the provider has no push
 * registration at all (the only case "not supported" is said), or why it failed — the classified error in the
 * viewer's language and the provider's own reason under it (the route is admin-only, so the server sends it).
 */
function RegistrationResult({ result }: { result: DelegationWebhookRegistrationResult }) {
  const { t } = useI18n();
  const errorText = useErrorText();
  if (result.state === "registered") {
    return <>✅ {result.label}: {result.detail}</>;
  }
  if (result.state === "not_supported") {
    return <>⚪ {result.label}: {t("settings.webhookNotSupported")}</>;
  }
  return (
    <>
      ❌ {result.label}: {errorText(result.error_code, result.error_message)}
      {result.error_detail && (
        <span className="mt-0.5 block break-words rounded-sm bg-muted px-2 py-1 font-mono">{result.error_detail}</span>
      )}
    </>
  );
}

/** Action widget of the "reconciliation" settings group: asks every download provider to register the push callback. */
export function RegisterWebhook() {
  const { t } = useI18n();
  const [results, setResults] = useState<DelegationWebhookRegistrationResult[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await registerDelegationWebhooks();
      setResults(r);
      if (r.some((x) => x.state === "registered")) {
        toast.success(t("settings.webhookRegistered"));
      } else if (r.some((x) => x.state === "failed")) {
        toast.error(t("settings.webhookNoneAccepted"));
      } else {
        toast.error(t("settings.webhookNoneSupported"));
      }
    } catch {
      setResults(null);
      setError(t("settings.webhookRegistrationFailed"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rounded-lg border border-dashed p-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm">
          <div className="font-medium">{t("settings.registerWebhookTitle")}</div>
          <div className="text-xs text-muted-foreground">{t("settings.registerWebhookDescription")}</div>
        </div>
        <Button size="sm" variant="secondary" disabled={busy} onClick={run}>
          {busy ? t("settings.inProgress") : t("settings.register")}
        </Button>
      </div>
      {error && <div className="mt-2 break-all text-xs text-muted-foreground">{error}</div>}
      {results && (
        <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
          {results.map((r) => (
            <li key={r.provider}>
              <RegistrationResult result={r} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
