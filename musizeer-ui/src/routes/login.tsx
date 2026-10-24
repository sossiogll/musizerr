import { useState, type FormEvent } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ProviderLogin } from "@/components/accounts/ProviderLogin";
import { useAuth } from "@/contexts/AuthContext";
import { useI18n, type MessageKey } from "@/i18n";
import { classifyError, type ApiErrorKind } from "@/lib/api-errors";
import { safeRedirect } from "@/lib/safe-redirect";

// Invalid credentials and an unavailable server are different problems: the
// first needs another password, the second only a moment.
const LOGIN_ERROR: Record<ApiErrorKind, MessageKey> = {
  unauthorized: "auth.invalidCredentials",
  forbidden: "auth.accountDeactivated",
  client: "auth.authDisabled",
  unreachable: "auth.serverUnavailable",
  upstream: "auth.serverUnavailable",
};

export function loginErrorKey(error: unknown): MessageKey {
  return LOGIN_ERROR[classifyError(error).kind];
}

export function LoginPage() {
  const { login, authConfig } = useAuth();
  const { t } = useI18n();
  const navigate = useNavigate();
  const { redirect } = useSearch({ strict: false }) as { redirect?: string };

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await login(username, password);
      // `href`: the target may carry its own search (e.g. /search?q=x).
      navigate({ href: safeRedirect(redirect), replace: true });
    } catch (err: unknown) {
      setError(t(loginErrorKey(err)));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex items-center justify-center min-h-[60vh]">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>{t("auth.loginTitle")}</CardTitle>
          <CardDescription>{t("auth.loginSubtitle")}</CardDescription>
        </CardHeader>
        <form onSubmit={onSubmit}>
          <CardContent className="space-y-4">
            {error && (
              <p role="alert" className="text-sm text-destructive">{error}</p>
            )}
            <div className="space-y-2">
              <label htmlFor="username" className="text-sm font-medium">
                {t("auth.username")}
              </label>
              <Input
                id="username"
                type="text"
                autoComplete="username"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <label htmlFor="password" className="text-sm font-medium">
                {t("auth.password")}
              </label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
          </CardContent>
          <CardFooter className="flex-col gap-3">
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? t("auth.signingIn") : t("auth.signIn")}
            </Button>
            {authConfig?.registration_open && (
              <p className="text-sm text-muted-foreground">
                {t("auth.noAccount")}{" "}
                <Link to="/register" search={redirect ? { redirect } : {}} className="font-medium text-primary underline underline-offset-4">
                  {t("auth.registerLink")}
                </Link>
              </p>
            )}
          </CardFooter>
        </form>
        <CardFooter className="pt-0">
          <ProviderLogin redirect={redirect} />
        </CardFooter>
      </Card>
    </div>
  );
}
