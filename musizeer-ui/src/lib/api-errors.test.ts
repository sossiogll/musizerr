import { describe, it, expect } from "vitest";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { classifyError, isRetryable } from "./api-errors";

function httpError(status: number, data: unknown = {}): AxiosError {
  const response = { status, data, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
  return new AxiosError("request failed", String(status), undefined, undefined, response);
}

describe("classifyError", () => {
  it("no response at all (network down, DNS, CORS) is unreachable", () => {
    expect(classifyError(new AxiosError("Network Error", "ERR_NETWORK")).kind).toBe("unreachable");
  });

  it("a timeout is unreachable", () => {
    expect(classifyError(new AxiosError("timeout of 15000ms exceeded", "ECONNABORTED")).kind).toBe("unreachable");
  });

  it("a non-HTTP error is unreachable (nothing came back)", () => {
    expect(classifyError(new Error("boom")).kind).toBe("unreachable");
  });

  it.each([
    [401, "unauthorized"],
    [403, "forbidden"],
    [400, "client"],
    [404, "client"],
    [422, "client"],
  ])("HTTP %i is %s", (status, kind) => {
    expect(classifyError(httpError(status, { detail: "x" })).kind).toBe(kind);
  });

  it("a 5xx without the backend's error body comes from a proxy: unreachable", () => {
    expect(classifyError(httpError(502, "<html>Bad Gateway</html>")).kind).toBe("unreachable");
    expect(classifyError(httpError(500, { detail: "Internal Server Error" })).kind).toBe("unreachable");
  });

  it("a 5xx with the backend's error code is upstream: the API is alive", () => {
    const err = httpError(502, {
      detail: { error_code: "metadata_provider_upstream_error", error_message: "The provider answered with an error.", provider: "p" },
    });
    const classified = classifyError(err);
    expect(classified.kind).toBe("upstream");
    expect(classified.code).toBe("metadata_provider_upstream_error");
    expect(classified.body?.provider).toBe("p");
  });

  it("database_busy is a retryable unreachable, not an upstream failure", () => {
    const err = httpError(503, { detail: { error_code: "database_busy", error_message: "busy" } });
    expect(classifyError(err).kind).toBe("unreachable");
    expect(isRetryable(err)).toBe(true);
  });

  it("keeps the plain detail string", () => {
    expect(classifyError(httpError(404, { detail: "Album not found" })).detail).toBe("Album not found");
  });
});

describe("isRetryable", () => {
  it("retries what may change: unreachable and a provider temporarily unavailable", () => {
    expect(isRetryable(new AxiosError("Network Error", "ERR_NETWORK"))).toBe(true);
    expect(isRetryable(httpError(503, { detail: { error_code: "metadata_provider_unreachable", error_message: "" } }))).toBe(true);
  });

  it("never retries a 4xx or a provider that answered with an error", () => {
    expect(isRetryable(httpError(404))).toBe(false);
    expect(isRetryable(httpError(401))).toBe(false);
    expect(isRetryable(httpError(502, { detail: { error_code: "metadata_provider_upstream_error", error_message: "" } }))).toBe(false);
  });
});
