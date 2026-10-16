import { QueryClient } from "@tanstack/react-query";
import { classifyError } from "./api-errors";
import { PERSISTED_QUERY_ROOTS } from "./query-keys";
import { MAX_AGE_MS } from "./query-persistence";

/**
 * Retry only what may succeed later: an unreachable API (twice; the
 * connectivity monitor takes over after that) or a provider behind it that
 * is temporarily unavailable (once). A 4xx never changes on retry.
 */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  const { kind, status } = classifyError(error);
  if (kind === "unreachable") return failureCount < 2;
  if (kind === "upstream" && status === 503) return failureCount < 1;
  return false;
}

export function createQueryClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: {
      queries: { staleTime: 5 * 60 * 1000, retry: shouldRetry },
      // A mutation never waits for the connection to come back: the UI
      // disables actions while offline, and one that slips through fails
      // at once with an explanation instead of spinning.
      mutations: { networkMode: "always", retry: false },
    },
  });
  // The offline copy keeps these for as long as it is valid, even when no
  // page is showing them.
  for (const root of PERSISTED_QUERY_ROOTS) {
    client.setQueryDefaults([root], { gcTime: MAX_AGE_MS });
  }
  return client;
}
