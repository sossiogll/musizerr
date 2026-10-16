import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { ExternalLink, KeyRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CredentialFieldsForm } from "@/components/accounts/CredentialFieldsForm";
import { useAuth } from "@/contexts/AuthContext";
import { useI18n } from "@/i18n";
import { getLoginProviders, loginWithProviderCredentials } from "@/lib/api-client";
import { providerLoginErrorCode, startProviderLogin } from "@/lib/provider-login";
import { queryKeys } from "@/lib/query-keys";
import { safeRedirect } from "@/lib/safe-redirect";
import type { LoginProvider } from "@/types";

/**
 * "Continue with …" for each provider the server says can sign users in
 * (GET /auth/account/login-providers): nothing here knows a provider. An
 * OAuth2 one sends the browser to the server, which sends it on to the
 * provider; one with a login account opens the form the provider declares
 * (`credential_fields`). Renders nothing while there is none.
 */
export function ProviderLogin({ redirect }: { redirect?: string }) {
  const { t } = useI18n();
  const { data } = useQuery({ queryKey: queryKeys.loginProviders, queryFn: getLoginProviders, staleTime: 60_000 });
  const [open, setOpen] = useState<string | null>(null);
  if (!data?.length) return null;

  return (
    <div className="w-full space-y-3">
      <div className="flex items-center gap-3 text-xs text-muted-foreground" aria-hidden>
        <span className="h-px flex-1 bg-border" />
        {t("auth.providerLogin.or")}
        <span className="h-px flex-1 bg-border" />
      </div>
      {data.map((provider) =>
        provider.auth_flow === "oauth2" ? (
          <Button
            key={provider.name}
            type="button"
            variant="outline"
            className="w-full"
            onClick={() => startProviderLogin(provider.name, redirect)}
          >
            <ExternalLink aria-hidden className="mr-1.5 size-4" />
            {t("auth.providerLogin.continueWith", { provider: provider.label })}
          </Button>
        ) : (
          <div key={provider.name} className="space-y-2">
            <Button
              type="button"
              variant="outline"
              className="w-full"
              aria-expanded={open === provider.name}
              onClick={() => setOpen(open === provider.name ? null : provider.name)}
            >
              <KeyRound aria-hidden className="mr-1.5 size-4" />
              {t("auth.providerLogin.continueWith", { provider: provider.label })}
            </Button>
            {open === provider.name && <ProviderLoginForm provider={provider} redirect={redirect} />}
          </div>
        ),
      )}
    </div>
  );
}

function ProviderLoginForm({ provider, redirect }: { provider: LoginProvider; redirect?: string }) {
  const { t, tx } = useI18n();
  const { startSession } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit(values: Record<string, string>) {
    setError(null);
    setSaving(true);
    try {
      await startSession(await loginWithProviderCredentials(provider.name, values));
      navigate({ href: safeRedirect(redirect), replace: true });
    } catch (err: unknown) {
      setError(tx(`auth.providerLogin.error.${providerLoginErrorCode(err)}`, t("auth.providerLogin.error.generic")));
    } finally {
      setSaving(false);
    }
  }

  return (
    <CredentialFieldsForm
      fields={provider.credential_fields}
      saving={saving}
      error={error}
      hint={t("auth.providerLogin.formHint", { provider: provider.label })}
      submitLabel={t("auth.signIn")}
      onSubmit={(values) => void submit(values)}
      onEdit={() => setError(null)}
    />
  );
}
