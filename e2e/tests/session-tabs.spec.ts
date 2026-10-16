import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { REFRESH_KEY, signIn, watchSession } from "./support";

// Every tab shares ONE refresh token and the server rotates it on each use.
// A dev-server restart reloads every tab at once: each restores the session
// with the same token. They must all stay signed in.

async function openSignedInTabs(context: BrowserContext, count: number): Promise<Page[]> {
  const token = await signIn(context.request);
  const first = await context.newPage();
  await first.goto("/");
  await first.evaluate(([key, value]) => localStorage.setItem(key, value), [REFRESH_KEY, token] as const);
  await first.close();
  return Promise.all(Array.from({ length: count }, () => context.newPage()));
}

async function loadTogether(pages: Page[], load: (page: Page) => Promise<unknown>) {
  const signedIn = pages.map((page) =>
    page.waitForResponse((r) => r.url().includes("/api/auth/account/me") && r.status() === 200),
  );
  await Promise.all(pages.map(load));
  await Promise.all(signedIn);
}

for (const locks of [true, false]) {
  test(`two tabs refreshing the session at the same time stay signed in (${locks ? "Web Locks" : "no Web Locks: server grace window"})`, async ({ browser }) => {
    const context = await browser.newContext();
    if (!locks) {
      await context.addInitScript(() => Object.defineProperty(navigator, "locks", { value: undefined, configurable: true }));
    }
    const pages = await openSignedInTabs(context, 2);
    const sessions = pages.map(watchSession);

    await loadTogether(pages, (page) => page.goto("/"));
    // The restart case: both reload at once, again.
    await loadTogether(pages, (page) => page.reload());

    for (const [index, page] of pages.entries()) {
      expect(await page.evaluate((key) => localStorage.getItem(key), REFRESH_KEY), `tab ${index}`).not.toBeNull();
      expect(sessions[index].refreshes.length, `tab ${index} refreshed`).toBeGreaterThan(0);
      expect(sessions[index].refreshes.every((status) => status === 200), `tab ${index}: ${sessions[index].refreshes}`).toBe(true);
      expect(sessions[index].signOuts, `tab ${index} signed out`).toEqual([]);
    }
    await context.close();
  });
}
