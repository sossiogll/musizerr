import { expect, test, type APIRequestContext } from "@playwright/test";
import { REFRESH_KEY, account, adminAccount, login } from "./support";

// The approval policy, end to end, with two people at once: a user asks for the
// whole discography of an artist whose provider can give one (a kind that
// always waits under the default policy), an admin
// who has the app open anywhere sees the badge on Administration, opens the
// request and denies it with a reason, and the user reads the outcome without
// reloading. Denying is what makes the spec safe on a real instance: nothing
// is ever queued. Whatever the spec leaves waiting (a failure half way) is
// cancelled in the end, and at the start (a previous run that died).

interface Tokens {
  access_token: string;
  refresh_token: string;
}

const bearer = (tokens: Tokens) => ({ Authorization: `Bearer ${tokens.access_token}` });
const REASON = "e2e: not this week";
/** The capability of a metadata provider that lists an artist's releases and their tracks (sdk/src/musizeer_sdk/capabilities.py). */
const DISCOGRAPHY = "discography";

/** The viewer's requests that still wait for an admin. */
async function waiting(request: APIRequestContext, tokens: Tokens): Promise<{ id: string; title: string }[]> {
  const response = await request.get("/api/download-requests?status=pending&limit=200", { headers: bearer(tokens) });
  return (await response.json()).items;
}

async function cancelWaiting(request: APIRequestContext, tokens: Tokens) {
  for (const { id } of await waiting(request, tokens)) {
    await request.post(`/api/download-requests/${id}/cancel`, { headers: bearer(tokens) });
  }
}

const badge = (count: number) => `${count} request${count === 1 ? "" : "s"} waiting for approval`;

test("a request waits, the admin sees the badge and denies it, the user reads the outcome", async ({ browser }) => {
  test.setTimeout(120_000);
  const admin = adminAccount();
  test.skip(!admin, "needs the e2e administrator: run the suite with e2e/run.sh");

  const userContext = await browser.newContext();
  const adminContext = await browser.newContext();
  const user = await login(userContext.request);
  const root = await login(adminContext.request, admin!);
  try {
    await cancelWaiting(userContext.request, user);

    // --- the policy must make this request wait, and there must be something to ask for
    const status = await (await userContext.request.get("/api/auth/account/status", { headers: bearer(user) })).json();
    test.skip(
      !(status.download_policy?.applies && status.download_policy.rules.artist.auto_up_to === 0),
      "the e2e user's policy lets a whole artist go through at once: nothing would wait",
    );
    // The whole discography can only be asked of an artist whose provider gives one: the providers
    // declare it (`discography`), and the artist page says so (`discography_downloadable`).
    const providers = await (await userContext.request.get("/api/providers/public")).json();
    const trending = await (await userContext.request.get("/api/discover/trending/artists?limit=10", { headers: bearer(user) })).json();
    const artist = trending.items.find((item: { provider: string }) => providers.metadata_capabilities[item.provider]?.includes(DISCOGRAPHY));
    test.skip(!artist, "needs a trending artist whose provider can give a discography");
    const path = `${encodeURIComponent(artist.provider)}/${encodeURIComponent(artist.provider_id)}`;
    const detail = await (await userContext.request.get(`/api/artist/${path}`, { headers: bearer(user) })).json();
    expect(detail.discography_downloadable, "the server offers the discography of this artist").toBe(true);
    const discography = await (await userContext.request.get(`/api/artist/${path}/albums`, { headers: bearer(user) })).json();
    const library = discography.library;
    test.skip(library && library.total !== null && library.downloaded >= library.total, "everything of that artist is in the library already");
    const before = (await (await adminContext.request.get("/api/admin/download-requests/pending-count", { headers: bearer(root) })).json()).pending as number;

    // --- both people are in the app (one navigation each: the refresh token rotates)
    await userContext.addInitScript(([key, value]) => localStorage.setItem(key, value), [REFRESH_KEY, user.refresh_token] as const);
    await adminContext.addInitScript(([key, value]) => localStorage.setItem(key, value), [REFRESH_KEY, root.refresh_token] as const);
    const userPage = await userContext.newPage();
    const adminPage = await adminContext.newPage();
    await adminPage.goto("/");
    await expect(adminPage.getByRole("link", { name: /Administration/ }).first()).toBeVisible();

    // --- the user asks: the button says Request, the request waits
    await userPage.goto(`/artist/${path}`);
    const ask = userPage.getByRole("button", { name: /^Request .* \(needs approval\) and add it to favorites$/ }).first();
    await ask.click();
    await userPage.getByRole("alertdialog").getByRole("button", { name: "Send request" }).click();
    await expect(userPage.getByText(/^Sent for approval: \d+ tracks? to download$/)).toBeVisible();
    await expect(userPage.getByRole("status").filter({ hasText: "Waiting for approval" })).toBeVisible();
    const [asked] = await waiting(userContext.request, user);
    expect(asked, "the server holds the request").toBeDefined();

    // --- the admin sees it arrive, on a page that has nothing to do with requests
    await expect(adminPage.getByLabel(badge(before + 1)).first()).toBeVisible();
    await adminPage.getByRole("link", { name: /Administration/ }).first().click();
    await adminPage.getByRole("navigation", { name: "Administration sections" }).getByRole("link", { name: /Requests/ }).click();
    const row = adminPage
      .getByRole("listitem")
      .filter({ hasText: asked.title })
      .filter({ hasText: `Asked by ${account().username}` })
      .first();
    await expect(row).toBeVisible();

    // --- the admin denies it, with a reason
    await row.getByRole("button", { name: `Deny ${asked.title}` }).click();
    const dialog = adminPage.getByRole("alertdialog");
    await dialog.getByLabel("Reason, shown to the requester (optional)").fill(REASON);
    await dialog.getByRole("button", { name: "Deny", exact: true }).click();
    await expect(adminPage.getByRole("status").filter({ hasText: "1 denied" })).toBeVisible();
    await expect(adminPage.getByLabel(badge(before + 1))).toHaveCount(0);

    // --- the user reads the outcome, live
    await expect(userPage.getByText(`Your request was denied: ${asked.title}`)).toBeVisible();
    await expect(userPage.getByText(`Reason: ${REASON}`).first()).toBeVisible();
    await expect(ask).toBeVisible();

    // --- and finds it in Downloads › Requests
    await userPage.getByRole("link", { name: /Downloads/ }).first().click();
    await userPage.getByRole("button", { name: /^Requests/ }).click();
    const mine = userPage.getByRole("listitem").filter({ hasText: asked.title }).first();
    await expect(mine).toContainText("Denied");
    await expect(mine).toContainText(`Reason: ${REASON}`);
  } finally {
    await cancelWaiting(userContext.request, user).catch(() => undefined);
    await userContext.close();
    await adminContext.close();
  }
});
