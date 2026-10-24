import { beforeEach, describe, expect, it, vi } from "vitest";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { providerLoginErrorCode, rememberLoginRedirect, startProviderLogin, takeLoginRedirect } from "./provider-login";

function httpError(status: number, data: unknown) {
  const response = { status, data, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
  return new AxiosError("failed", String(status), undefined, undefined, response);
}

beforeEach(() => sessionStorage.clear());

describe("the way back from a provider", () => {
  it("remembers the target once and forgets it when taken", () => {
    rememberLoginRedirect("/album/p/1");

    expect(takeLoginRedirect()).toBe("/album/p/1");
    expect(takeLoginRedirect()).toBe("/");
  });

  it("remembering nothing clears an older target", () => {
    rememberLoginRedirect("/album/p/1");
    rememberLoginRedirect(undefined);

    expect(takeLoginRedirect()).toBe("/");
  });

  it("only a path inside the app is ever followed", () => {
    for (const target of ["https://evil.example/", "//evil.example", "/login", "javascript:alert(1)"]) {
      sessionStorage.setItem("musizeer.login-redirect", target);
      expect(takeLoginRedirect(), target).toBe("/");
    }
  });

  it("blocked storage means Home, never an error", () => {
    const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });

    expect(takeLoginRedirect()).toBe("/");
    spy.mockRestore();
  });

  it("starting remembers the target before the browser leaves", () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });

    startProviderLogin("orbit", "/album/p/1");

    expect(assign).toHaveBeenCalledWith("/api/auth/account/login/orbit/start");
    expect(takeLoginRedirect()).toBe("/album/p/1");
    vi.unstubAllGlobals();
  });
});

describe("providerLoginErrorCode", () => {
  it("is the code the server sent", () => {
    expect(providerLoginErrorCode(httpError(403, { detail: { error_code: "account_pending", error_message: "x" } }))).toBe("account_pending");
  });

  it("an answer without a code is a refusal, and no answer is an unavailable provider", () => {
    expect(providerLoginErrorCode(httpError(401, { detail: "nope" }))).toBe("provider_login_refused");
    expect(providerLoginErrorCode(new AxiosError("Network Error", "ERR_NETWORK"))).toBe("provider_login_unavailable");
  });
});
