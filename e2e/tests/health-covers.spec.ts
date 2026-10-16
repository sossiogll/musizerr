import { expect, test } from "@playwright/test";

// A page full of covers the providers cannot serve must not starve the
// requests that matter: the browser opens only a few connections per host,
// and /api/health decides whether the app says "offline".
test("10 broken covers do not block /api/health", async ({ page }) => {
  await page.goto("/");
  const providers: string[] = (await (await page.request.get("/api/providers/public")).json()).providers.map(
    (p: { id: string }) => p.id,
  );
  expect(providers.length).toBeGreaterThan(0);

  const elapsed = await page.evaluate(async (names) => {
    for (let i = 0; i < 10; i++) {
      const img = document.createElement("img");
      img.src = `/api/cover/provider/${encodeURIComponent(names[i % names.length])}/e2e-broken-${Date.now()}-${i}`;
      document.body.appendChild(img);
    }
    const started = performance.now();
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) throw new Error(`health answered ${response.status}`);
    return performance.now() - started;
  }, providers);

  expect(elapsed).toBeLessThan(3_000);
});
