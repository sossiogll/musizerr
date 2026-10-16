import { describe, expect, it } from "vitest";
import { safeRedirect, signInSearch } from "./safe-redirect";

describe("safeRedirect", () => {
  it.each([
    ["/library/saved", "/library/saved"],
    ["/album/p/1?x=1", "/album/p/1?x=1"],
  ])("keeps an in-app path %s", (input, out) => {
    expect(safeRedirect(input)).toBe(out);
  });

  it.each([
    "https://evil.example/",
    "//evil.example/x",
    "/\\evil.example",
    "javascript:alert(1)",
    "",
    undefined,
    42,
    "/login",
    "/login?redirect=/x",
    "/register",
    "/register?redirect=/x",
    "/login#x",
    "/ok\nSet-Cookie: x",
  ])("falls back to Home for %s", (input) => {
    expect(safeRedirect(input)).toBe("/");
  });
});

describe("signInSearch", () => {
  it("brings the viewer back to the page they were on, with its search", () => {
    expect(signInSearch("/album/p/1")).toEqual({ redirect: "/album/p/1" });
    expect(signInSearch("/search?q=korn")).toEqual({ redirect: "/search?q=korn" });
  });

  it("says nothing for Home", () => {
    expect(signInSearch("/")).toEqual({});
  });

  it("on an auth page keeps the redirect it carries, never the auth page itself", () => {
    expect(signInSearch("/login?redirect=%2Fx", "/favorites")).toEqual({ redirect: "/favorites" });
    expect(signInSearch("/register", undefined)).toEqual({});
    expect(signInSearch("/register?redirect=x", "https://evil.example")).toEqual({});
  });

  it("never carries another origin", () => {
    expect(signInSearch("//evil.example/x")).toEqual({});
  });
});
