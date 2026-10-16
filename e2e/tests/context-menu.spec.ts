import { expect, test } from "@playwright/test";
import { REFRESH_KEY, signIn } from "./support";

// The contextual menu of a track, in the real app: the "⋯" button and a right
// click open the same menu, Escape closes it and gives the focus back, and
// "Add to queue" puts the track in the queue the player shows.

interface ListedTrack {
  title: string;
  playback?: { type: string };
}

test("track menu: the button and a right click open the same menu; Escape returns the focus; Add to queue queues", async ({ page, context }) => {
  // Signed in from the first load (one navigation only: the token rotates).
  const token = await signIn(context.request);
  await context.addInitScript(([key, value]) => localStorage.setItem(key, value), [REFRESH_KEY, token] as const);

  const trending = page.waitForResponse(
    (r) => r.url().includes("/api/discover/trending/tracks") && r.ok() && r.request().headers()["authorization"] !== undefined,
  );
  await page.goto("/");
  const { items } = (await (await trending).json()) as { items: ListedTrack[] };
  const track = items.slice(0, 10).find((t) => t.playback?.type && t.playback.type !== "none");
  test.skip(!track, "needs a playable trending track on Home");
  const title = track!.title;

  // --- the button
  const more = page.getByRole("button", { name: `More actions for ${title}`, exact: true }).first();
  await more.click();
  const menu = page.getByRole("menu", { name: `More actions for ${title}` });
  await expect(menu).toBeVisible();
  const entries = await menu.getByRole("menuitem").allTextContents();
  expect(entries).toEqual(expect.arrayContaining(["Play next", "Add to queue"]));

  // --- Escape closes it and the button has the focus again
  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await expect(more).toBeFocused();

  // --- a right click on the row: the same menu
  await page.getByRole("link", { name: title, exact: true }).first().click({ button: "right" });
  await expect(menu).toBeVisible();
  expect(await menu.getByRole("menuitem").allTextContents()).toEqual(entries);

  // --- Add to queue: the track is in the queue panel
  await menu.getByRole("menuitem", { name: "Add to queue" }).click();
  await expect(menu).toBeHidden();
  const player = page.getByRole("region", { name: "Player" });
  await player.getByRole("button", { name: /^Queue \(\d+\)$/ }).click();
  await expect(page.getByRole("region", { name: "Up next" })).toContainText(title);
});
