import { useEffect, type ReactNode } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useCapabilities } from "@/hooks/useCapabilities";
import { safeRedirect } from "@/lib/safe-redirect";

/**
 * Guard of the pages that only make sense when authentication is on (sign in, register, the return of a
 * sign-in through a provider): with it off there are no accounts and nobody signs in, so a visitor who
 * lands here (a bookmark, the page they were on when authentication went off) is sent to where they came
 * from (`?redirect=`, checked) or Home. The mode is the server's (`mode: "open"`); nothing is derived here.
 */
export function OpenModeRedirect({ children }: { children: ReactNode }) {
  const { mode } = useCapabilities();
  const { redirect } = useSearch({ strict: false }) as { redirect?: string };
  const navigate = useNavigate();
  const open = mode === "open";
  useEffect(() => {
    // `href`: the target may carry its own search (e.g. /search?q=x).
    if (open) void navigate({ href: safeRedirect(redirect), replace: true });
  }, [open, redirect, navigate]);
  return open ? null : <>{children}</>;
}
