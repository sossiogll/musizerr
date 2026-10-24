import { useState } from "react";
import { CheckCircle2, Loader2, XCircle, Zap } from "lucide-react";
import { testProviderConnection } from "@/lib/api-client";
import { useI18n } from "@/i18n";
import { Button } from "@/components/ui/button";
import type { AccountTarget } from "@/types";

/**
 * Tests a provider and says the outcome in words: the OAuth2 app client
 * (`scope` "app", admins), or an account (`scope` "account", the default):
 * the viewer's own, the shared one, or — without a `target` — the account the
 * viewer's own calls would use.
 */
export function TestButton({ provider, scope = "account", target }: { provider: string; scope?: "app" | "account"; target?: AccountTarget }) {
  const { t } = useI18n();
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; detail: string } | null>(null);

  const handleTest = async () => {
    setLoading(true);
    setResult(null);
    try {
      const res = await testProviderConnection(provider, scope, target);
      const r = scope === "app" ? res.app : res.account;
      setResult(r ?? { ok: false, detail: t("providersTab.noTestAvailable") });
    } catch {
      setResult({ ok: false, detail: t("providersTab.requestFailed") });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex items-center gap-2">
      <Button variant="outline" size="sm" onClick={handleTest} disabled={loading}>
        {loading ? <Loader2 className="w-3.5 h-3.5 mr-1.5 animate-spin" /> : <Zap className="w-3.5 h-3.5 mr-1.5" />}
        {t("providersTab.test")}
      </Button>
      {result && (
        <span className={`flex items-center gap-1 text-xs ${result.ok ? "text-green-500" : "text-destructive"}`}>
          {result.ok ? <CheckCircle2 className="w-3.5 h-3.5" /> : <XCircle className="w-3.5 h-3.5" />}
          {result.detail}
        </span>
      )}
    </div>
  );
}
