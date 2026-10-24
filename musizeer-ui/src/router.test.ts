import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

describe("router — one chunk per page", () => {
  it("every page chunk loads (what preloading fetches once the app is up)", async () => {
    const { preloadPages } = await import("./router");
    const results = await preloadPages();
    expect(results.length).toBeGreaterThan(10);
    expect(results.filter((r) => r.status === "rejected")).toEqual([]);
  });

  it("the route tree is built without loading any page", async () => {
    const { router } = await import("./router");
    const paths = Object.keys(router.routesByPath);
    expect(paths).toEqual(expect.arrayContaining(["/", "/search", "/track/$provider/$id", "/downloads", "/admin/users"]));
  });
});
