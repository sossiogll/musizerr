import { useState, type ReactNode } from "react";
import { Link, Navigate, useRouterState } from "@tanstack/react-router";
import { ShieldOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useCapabilities, type Capability } from "@/hooks/useCapabilities";
import { useI18n } from "@/i18n";

/**
 * Route guard: renders the page only if the viewer has `capability` (or,
 * given a list, any of them). A guest
 * is sent to sign in (and brought back afterwards); a signed-in viewer who
 * lacks it sees why, instead of a blank page.
 */
export function RequireCapability({
  capability,
  children,
}: {
  capability: Capability | readonly Capability[];
  children: ReactNode;
}) {
  const { mode, can } = useCapabilities();
  // The page the viewer asked for, captured when the guard first renders:
  // once the redirect below starts, the router location is already /login,
  // and reading it again would chain /login?redirect=/login?redirect=...
  const current = useRouterState({ select: (s) => s.location.href });
  const [href] = useState(current);
  const wanted: readonly Capability[] = typeof capability === "string" ? [capability] : capability;
  if (wanted.some((c) => can[c])) return <>{children}</>;
  if (mode === "guest") return <Navigate to="/login" search={{ redirect: href }} />;
  return <Forbidden />;
}

export function Forbidden() {
  const { t } = useI18n();
  return (
    <section role="alert" className="mx-auto flex max-w-lg flex-col items-start gap-4 py-16">
      <ShieldOff aria-hidden className="size-8 text-muted-foreground" />
      <h1 className="font-display text-2xl font-bold">{t("errors.forbiddenTitle")}</h1>
      <p className="text-muted-foreground">{t("errors.forbidden")}</p>
      <Button asChild variant="outline">
        <Link to="/">{t("errors.goHome")}</Link>
      </Button>
    </section>
  );
}
