import { expect, test } from "@playwright/test";
import { REFRESH_KEY, adminAccount, login } from "./support";

// Opening a guarded page directly, in a new page, right after the session is
// restored: the saved copy of the app holds the status of a GUEST (an earlier
// visit before signing in), and the restored session must not be judged on
// it. The page has to stay where it was asked for, never pass through sign-in.

/** Where the frontend keeps its offline copy (lib/query-persistence.ts). */
const CACHE_KEY = "musizerr.query-cache";

test("a deep link to an administration page right after the session is restored does not go to sign in", async ({ browser }) => {
  const admin = adminAccount();
  test.skip(!admin, "needs the e2e administrator: run the suite with e2e/run.sh");

  const context = await browser.newContext();
  try {
    // --- an earlier visit as a guest leaves the guest's status in the saved copy
    const guest = await context.newPage();
    await guest.goto("/");
    await expect
      .poll(() => guest.evaluate((key) => localStorage.getItem(key) ?? "", CACHE_KEY), { message: "the guest's status is saved" })
      .toContain("auth-config");
    // --- then the administrator signs in (one navigation uses the refresh token: it rotates)
    const { refresh_token } = await login(context.request, admin!);
    await guest.evaluate(([key, value]) => localStorage.setItem(key, value), [REFRESH_KEY, refresh_token] as const);
    await guest.close();

    // --- a new page opens the guarded address directly
    const page = await context.newPage();
    const visited: string[] = [];
    page.on("framenavigated", (frame) => {
      if (frame === page.mainFrame()) visited.push(new URL(frame.url()).pathname);
    });
    await page.goto("/admin/requests");

    await expect(page.getByRole("navigation", { name: "Administration sections" })).toBeVisible();
    await expect(page).toHaveURL(/\/admin\/requests$/);
    expect(visited, "never through sign-in").not.toContain("/login");
  } finally {
    await context.close();
  }
});
