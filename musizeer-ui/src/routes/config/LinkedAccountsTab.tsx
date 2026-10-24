import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, ExternalLink, Info } from "lucide-react";
import { getLinkedAccounts, getOAuthAuthorizeUrl, revokeAuth, saveCredentials, testProviderConnection } from "@/lib/api-client";
import { useAuthReturn } from "@/hooks/useAuthReturn";
import { classifyError } from "@/lib/api-errors";
import { useI18n } from "@/i18n";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CredentialFieldsForm } from "@/components/accounts/CredentialFieldsForm";
import { TestButton } from "@/components/accounts/TestButton";
import type { LinkedAccount } from "@/types";

/**
 * Settings › Linked accounts: the providers the viewer may connect their OWN
 * account to, and what each connection enables. Everything comes from
 * GET /auth/linked-accounts, which lists only the providers whose declared
 * account model has personal accounts — never a provider name written here.
 * The instance's shared accounts are an administrator's, in
 * Administration › Providers.
 */
export function LinkedAccountsTab() {
  const { t } = useI18n();
  useAuthReturn();
  const { data, isLoading, isError } = useQuery({ queryKey: ["linked-accounts"], queryFn: getLinkedAccounts });

  if (isLoading) {
    return (
      <div className="space-y-3 mt-4">
        {Array.from({ length: 2 }).map((_, i) => (
          <div key={i} className="h-24 rounded-lg bg-muted animate-pulse" />
        ))}
      </div>
    );
  }
  if (isError || !data) {
    return <p className="mt-4 text-sm text-muted-foreground">{t("linkedAccounts.loadFailed")}</p>;
  }

  return (
    <div className="mt-4 space-y-4">
      <p className="text-sm text-muted-foreground">{t("linkedAccounts.intro")}</p>
      {data.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("linkedAccounts.empty")}</p>
      ) : (
        <div className="space-y-3">
          {data.map((account) => (
            <LinkedAccountCard key={account.name} account={account} />
          ))}
        </div>
      )}
    </div>
  );
}

function LinkedAccountCard({ account }: { account: LinkedAccount }) {
  const { t, tx } = useI18n();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { personal, shared } = account;
  const missing = personal.connected ? personal.missing_permissions : [];

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["linked-accounts"] });
    void queryClient.invalidateQueries({ queryKey: ["providers"] });
  };

  const disconnect = useMutation({
    mutationFn: () => revokeAuth(account.name, "personal"),
    onSuccess: () => {
      refresh();
      toast.success(t("linkedAccounts.disconnected", { label: account.label }));
    },
    onError: () => toast.error(t("linkedAccounts.disconnectFailed")),
  });

  // Save, then test: an account that does not work is not kept.
  const connectLogin = useMutation({
    mutationFn: async (values: Record<string, string>) => {
      setError(null);
      await saveCredentials(account.name, values, "personal");
      const result = (await testProviderConnection(account.name, "account", "personal")).account;
      if (result && !result.ok) {
        await revokeAuth(account.name, "personal");
        throw new Error(result.detail);
      }
    },
    onSuccess: () => {
      refresh();
      setEditing(false);
      toast.success(t("linkedAccounts.connectedToast", { label: account.label }));
    },
    onError: (e: Error) => {
      refresh();
      // Another user already linked this account: say so, rather than the transport's text.
      const { status } = classifyError(e);
      setError(status === 409 ? t("linkedAccounts.alreadyLinked") : status === 422 ? t("linkedAccounts.notRecognised") : e.message);
    },
  });

  const authorize = async () => {
    try {
      window.location.href = await getOAuthAuthorizeUrl(account.name, "personal");
    } catch {
      toast.error(t("providersTab.authorizationFailed"));
    }
  };

  const status = personal.connected
    ? personal.username
      ? t("linkedAccounts.connectedAs", { username: personal.username })
      : t("linkedAccounts.connected")
    : shared?.connected
      ? t("linkedAccounts.usingShared")
      : t("linkedAccounts.notConnected");

  return (
    <Card>
      <CardHeader className="p-4 pb-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-sm">{account.label}</CardTitle>
          <Badge variant={personal.connected ? "default" : "secondary"}>{status}</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3 p-4 pt-0">
        {personal.capabilities.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {t("linkedAccounts.enables")}: {personal.capabilities.map((c) => tx(`linkedAccounts.capability.${c}`, c)).join(", ")}
          </p>
        )}
        {personal.connected && personal.login === true && (
          <p className="text-xs text-muted-foreground">{t("linkedAccounts.loginReady")}</p>
        )}
        {personal.connected && personal.login === false && (
          <p role="status" className="text-xs text-amber-600 dark:text-amber-500 flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
            {t("linkedAccounts.loginReconnect")}
          </p>
        )}
        {!personal.connected && !shared?.connected && account.account_model === "personal_with_shared" && (
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Info className="w-3.5 h-3.5 shrink-0" />
            {t("linkedAccounts.noSharedAccount")}
          </p>
        )}
        {!personal.connected && shared?.connected && (
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Info className="w-3.5 h-3.5 shrink-0" />
            {t("linkedAccounts.sharedHint")}
          </p>
        )}
        {/* The connection lacks a permission the provider now asks for: say what reconnecting allows, by capability key. */}
        {missing.map((capability) => (
          <p key={capability} role="status" className="text-xs text-amber-600 dark:text-amber-500 flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
            {tx(`linkedAccounts.missingPermission.${capability}`, t("linkedAccounts.missingPermissionGeneric"))}
          </p>
        ))}
        {!account.connectable && <p className="text-xs text-muted-foreground">{t("linkedAccounts.notConnectable")}</p>}

        {account.auth_flow === "oauth2" ? (
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" disabled={!account.connectable} onClick={authorize}>
              <ExternalLink className="w-3.5 h-3.5 mr-1.5" />
              {missing.length > 0 ? t("linkedAccounts.reconnect") : personal.connected ? t("linkedAccounts.change") : t("linkedAccounts.connect")}
            </Button>
            {personal.connected && <DisconnectButton onClick={() => disconnect.mutate()} disabled={disconnect.isPending} />}
          </div>
        ) : editing || !personal.connected ? (
          <CredentialFieldsForm
            fields={account.credential_fields}
            saving={connectLogin.isPending}
            error={error}
            onSubmit={(values) => connectLogin.mutate(values)}
            onCancel={personal.connected ? () => { setEditing(false); setError(null); } : undefined}
            onEdit={() => setError(null)}
          />
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
              {t("linkedAccounts.change")}
            </Button>
            <DisconnectButton onClick={() => disconnect.mutate()} disabled={disconnect.isPending} />
          </div>
        )}

        {personal.connected && <TestButton provider={account.name} target="personal" />}
      </CardContent>
    </Card>
  );
}

function DisconnectButton({ onClick, disabled }: { onClick: () => void; disabled: boolean }) {
  const { t } = useI18n();
  return (
    <Button variant="outline" size="sm" className="text-destructive" onClick={onClick} disabled={disabled}>
      {t("linkedAccounts.disconnect")}
    </Button>
  );
}
