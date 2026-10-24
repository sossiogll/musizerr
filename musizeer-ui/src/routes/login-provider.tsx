import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/contexts/AuthContext";
import { useI18n } from "@/i18n";
import { exchangeLoginCode } from "@/lib/api-client";
import { providerLoginErrorCode, takeLoginRedirect } from "@/lib/provider-login";

/**
 * Where the browser lands when the authorization at a provider ends
 * (`/login/provider`, set by GET /auth/{provider}/callback): with a one-time
 * `?code=` it is exchanged for a session, with an `?error=` (a stable code)
 * the reason is shown. A session never travels in the address: the code works
 * once and is taken out of it at once.
 */
export function ProviderLoginReturnPage() {
  const { t, tx } = useI18n();
  const { startSession } = useAuth();
  const navigate = useNavigate();
  const { code, error } = useSearch({ strict: false }) as { code?: string; error?: string };
  const [failure, setFailure] = useState<string | null>(error ?? null);
  const exchanged = useRef(false);

  useEffect(() => {
    if (!code || exchanged.current) return;
    // The code is spent by its first use: a second run of this effect (React's
    // strict mode, a re-render) must not exchange it again and fail.
    exchanged.current = true;
    window.history.replaceState(window.history.state, "", window.location.pathname);
    exchangeLoginCode(code)
      .then(startSession)
      .then(() => navigate({ href: takeLoginRedirect(), replace: true }))
      .catch((err: unknown) => setFailure(providerLoginErrorCode(err)));
  }, [code, startSession, navigate]);

  if (!failure && code) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center" role="status">
        <Loader2 aria-hidden className="mr-2 size-4 animate-spin" />
        {t("auth.providerLogin.finishing")}
      </div>
    );
  }

  const pending = failure === "account_pending";
  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>{t(pending ? "auth.providerLogin.pendingTitle" : "auth.providerLogin.failedTitle")}</CardTitle>
          <CardDescription>{t("auth.providerLogin.subtitle")}</CardDescription>
        </CardHeader>
        <CardContent>
          <p role="alert" className="text-sm">
            {tx(`auth.providerLogin.error.${failure ?? "provider_login_refused"}`, t("auth.providerLogin.error.generic"))}
          </p>
        </CardContent>
        <CardFooter>
          <Button asChild className="w-full">
            <Link to="/login">{t("auth.backToLogin")}</Link>
          </Button>
        </CardFooter>
      </Card>
    </div>
  );
}
