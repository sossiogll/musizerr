import { useRouterState } from "@tanstack/react-router";
import { signInSearch } from "@/lib/safe-redirect";

/**
 * The search of a "Sign in" link: the page the viewer is on, so signing in
 * brings them back to it (`?redirect=`, checked again by the login route).
 */
export function useSignInSearch(): { redirect?: string } {
  const href = useRouterState({ select: (s) => s.location.href });
  const carried = useRouterState({
    select: (s) => (s.location.search as { redirect?: unknown }).redirect,
  });
  return signInSearch(href, typeof carried === "string" ? carried : undefined);
}
