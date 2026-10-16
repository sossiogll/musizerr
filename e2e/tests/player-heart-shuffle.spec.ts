import { expect, test } from "@playwright/test";
import { REFRESH_KEY, signIn } from "./support";

// The bottom player's heart and shuffle, in the real app: the heart of a
// library track toggles at once and the row of the same track follows it;
// shuffle keeps the track playing first and Next follows the shuffled order
// the queue panel shows.

interface ListedTrack {
  title: string;
  in_library?: boolean;
  favorite?: boolean;
  playback?: { type: string };
}

test("player: the heart toggles and is shared with the row; shuffle reorders what plays next", async ({ page, context }) => {
  // Signed in from the first load (one navigation only: the token rotates).
  const token = await signIn(context.request);
  await context.addInitScript(([key, value]) => localStorage.setItem(key, value), [REFRESH_KEY, token] as const);

  const trending = page.waitForResponse(
    (r) => r.url().includes("/api/discover/trending/tracks") && r.ok() && r.request().headers()["authorization"] !== undefined,
  );
  await page.goto("/");
  // Home shows the first 10 trending tracks.
  const { items } = (await (await trending).json()) as { items: ListedTrack[] };
  const playable = items.slice(0, 10).filter((t) => t.playback?.type !== "none");
  const track = playable.find((t) => t.in_library && !t.favorite && t.playback?.type === "full");
  test.skip(!track || playable.length < 3, "needs a trending track in the library, not a favorite yet, among 3 playable ones");
  const title = track!.title;

  await page.getByRole("button", { name: `Play ${title}`, exact: true }).first().click();
  const player = page.getByRole("region", { name: "Player" });
  await expect(player.getByRole("button", { name: "Pause" })).toBeVisible();

  // --- the heart
  const add = player.getByRole("button", { name: `Add ${title} to favorites`, exact: true });
  await expect(add).toHaveAttribute("aria-pressed", "false");
  await add.click();
  const remove = player.getByRole("button", { name: `Remove ${title} from favorites`, exact: true });
  await expect(remove).toHaveAttribute("aria-pressed", "true");
  // The row of the same track says the same, without a reload.
  await expect(page.getByRole("button", { name: `Remove ${title} from favorites`, exact: true })).toHaveCount(2);
  // Back as it was, for the next run.
  await remove.click();
  await expect(player.getByRole("button", { name: `Add ${title} to favorites`, exact: true })).toHaveAttribute("aria-pressed", "false");

  // --- shuffle
  const shuffle = player.getByRole("button", { name: "Shuffle" });
  await expect(shuffle).toHaveAttribute("aria-pressed", "false");
  await shuffle.click();
  await expect(shuffle).toHaveAttribute("aria-pressed", "true");

  await player.getByRole("button", { name: /^Queue \(\d+\)$/ }).click();
  const panel = page.getByRole("region", { name: "Up next" });
  const entries = panel.getByRole("listitem");
  // The track playing comes first; the rest in the shuffled order.
  await expect(entries.first().getByRole("button")).toHaveAttribute("aria-current", "true");
  await expect(entries.first()).toContainText(title);
  expect(await entries.count()).toBeGreaterThan(2);

  // Next plays the first of them that can play.
  const playableNext: string[] = [];
  for (const entry of (await entries.all()).slice(1)) {
    if (await entry.getByRole("button").isEnabled()) playableNext.push((await entry.textContent()) ?? "");
  }
  await page.keyboard.press("Escape");
  await player.getByRole("button", { name: "Next" }).click();
  await expect(player.getByTestId("player-track").getByRole("link").first()).not.toHaveText(title);
  const nextTitle = (await player.getByTestId("player-track").getByRole("link").first().textContent()) ?? "";
  expect(playableNext[0]).toContain(nextTitle);
  // Remembered on this device.
  expect(await page.evaluate(() => localStorage.getItem("musizerr-play-mode"))).toContain('"shuffle":true');
});
