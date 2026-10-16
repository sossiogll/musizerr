import { isAxiosError } from "axios";

/**
 * What went wrong with an API call, from the UI's point of view:
 *
 * - `unreachable`  the API did not answer (network error, timeout) or its
 *                  proxy answered 5xx without a body from the backend, or the
 *                  backend said it is busy (`database_busy`): retry later;
 * - `upstream`     the backend answered with a structured 5xx error of its own
 *                  (`detail.error_code`, e.g. a provider that failed): the API
 *                  is fine, a service behind it is not;
 * - `unauthorized` 401: no session, or it expired;
 * - `forbidden`    403: signed in, but not allowed;
 * - `client`       any other 4xx: the request itself was refused.
 */
export type ApiErrorKind = "unreachable" | "upstream" | "unauthorized" | "forbidden" | "client";

export interface ApiErrorBody {
  error_code?: string | null;
  error_message?: string | null;
  /** Raw error text: the backend sends it to admins only. */
  error_detail?: string;
  provider?: string;
  /** `playlist_full`: the most tracks a playlist may hold. */
  max_tracks?: number;
}

export interface ClassifiedError {
  kind: ApiErrorKind;
  status: number | null;
  /** Stable code from the backend (`detail.error_code`), when it sent one. */
  code: string | null;
  /** Plain `detail` string, or the structured body. */
  detail: string | null;
  body: ApiErrorBody | null;
}

const RETRYABLE_CODES = new Set(["database_busy"]);

function structuredBody(data: unknown): ApiErrorBody | null {
  if (!data || typeof data !== "object") return null;
  const detail = (data as { detail?: unknown }).detail;
  if (detail && typeof detail === "object" && "error_code" in detail) return detail as ApiErrorBody;
  return null;
}

function plainDetail(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const detail = (data as { detail?: unknown }).detail;
  return typeof detail === "string" ? detail : null;
}

export function classifyError(error: unknown): ClassifiedError {
  if (!isAxiosError(error) || !error.response) {
    // No response at all: network down, DNS, CORS, timeout, aborted.
    return { kind: "unreachable", status: null, code: null, detail: null, body: null };
  }
  const { status, data } = error.response;
  const body = structuredBody(data);
  const code = body?.error_code ?? null;
  const detail = plainDetail(data);
  const base = { status, code, detail, body };
  if (status === 401) return { kind: "unauthorized", ...base };
  if (status === 403) return { kind: "forbidden", ...base };
  if (status >= 500) {
    // A 5xx without the backend's own error body comes from a proxy in front
    // of a backend that is down or restarting; database_busy is retryable.
    const alive = body !== null && !(code !== null && RETRYABLE_CODES.has(code));
    return { kind: alive ? "upstream" : "unreachable", ...base };
  }
  return { kind: "client", ...base };
}

/** True when retrying the same request later may succeed. */
export function isRetryable(error: unknown): boolean {
  const { kind, status } = classifyError(error);
  return kind === "unreachable" || (kind === "upstream" && status === 503);
}
