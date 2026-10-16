import { expect, test } from "@playwright/test";

// A preview URL that expired while its list sat in a cache answers 403. The
// player must ask for the track again and play its fresh URL, not stay
// silent with a "Pause" button.
const EXPIRED = "/api/e2e-expired/preview.mp3";

test("play with an expired preview recovers", async ({ page }) => {
  let title: string | null = null;
  let served = 0;
  // The first answer of the trending tracks carries an expired URL for one
  // track; the next one (asked again by the player) is the real answer.
  await page.route("**/api/discover/trending/tracks**", async (route) => {
    served += 1;
    if (served > 1) return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    const track = body.items.find((t: { playback?: { type: string }; preview_url?: string }) =>
      t.playback?.type === "preview" && t.preview_url,
    );
    if (track) {
      title = track.title;
      track.preview_url = EXPIRED;
    }
    await route.fulfill({ response, json: body });
  });
  const expired: number[] = [];
  await page.route(`**${EXPIRED}`, (route) => {
    expired.push(Date.now());
    return route.fulfill({ status: 403, body: "expired" });
  });

  await page.goto("/");
  await expect.poll(() => title, { message: "a trending track with a preview" }).not.toBeNull();

  const fresh = page.waitForResponse(
    (r) => r.request().resourceType() === "media" && !r.url().includes(EXPIRED) && r.status() < 400,
  );
  await page.getByRole("button", { name: `Play a 30 second preview of ${title}` }).first().click();

  await fresh;
  expect(expired.length).toBeGreaterThan(0);
  expect(served).toBeGreaterThan(1);
  const player = page.getByRole("region", { name: "Player" });
  await expect(player.getByRole("button", { name: "Pause" })).toBeVisible();
  // It really plays: the position moves on.
  await expect
    .poll(async () => Number(await player.getByRole("slider", { name: "Position" }).first().inputValue()), {
      timeout: 20_000,
    })
    .toBeGreaterThan(0);
  await expect(page.getByText(/can't be played right now/)).toHaveCount(0);
});
