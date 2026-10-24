import { dehydrate, hydrate, type Query, type QueryClient } from "@tanstack/react-query";
import { PERSISTED_QUERY_ROOTS, VIEWER_QUERY_ROOTS } from "./query-keys";

/**
 * A copy of a few queries in localStorage, so the app can show the last
 * known data while the API is unreachable (and on a reload during the
 * outage). Only the roots in PERSISTED_QUERY_ROOTS are saved: the library,
 * the downloads, discover, the public provider labels and the viewer's own
 * status — never settings, credentials or admin data.
 *
 * The copy belongs to one viewer (`owner`): when a different viewer is
 * recognised, or on sign-out, it is thrown away.
 */

export const STORAGE_KEY = "musizeer.query-cache";
export const CACHE_VERSION = 1;
export const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const WRITE_THROTTLE_MS = 1_000;

interface Snapshot {
  v: number;
  savedAt: number;
  owner: string | null;
  state: ReturnType<typeof dehydrate>;
}

let owner: string | null = null;

/**
 * Audio URLs are saved only when the API serves them (a path under it): one
 * pointing elsewhere may be signed for a few minutes, and the copy lives
 * for days. The track keeps its `playback`; without the URL the player
 * treats it as not playable until the list is asked again.
 */
const MEDIA_URL_FIELDS: ReadonlySet<string> = new Set(["preview_url", "stream_url"]);

function withoutForeignMediaUrls(key: string, value: unknown): unknown {
  if (MEDIA_URL_FIELDS.has(key) && typeof value === "string" && !(value.startsWith("/") && !value.startsWith("//"))) {
    return null;
  }
  return value;
}

export function isPersistedQuery(query: Pick<Query, "queryKey" | "state">): boolean {
  const root = query.queryKey[0];
  return typeof root === "string" && PERSISTED_QUERY_ROOTS.has(root) && query.state.status === "success";
}

function read(): Snapshot | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const snapshot = JSON.parse(raw) as Snapshot;
    if (snapshot.v !== CACHE_VERSION || Date.now() - snapshot.savedAt > MAX_AGE_MS) return null;
    return snapshot;
  } catch {
    return null;
  }
}

function write(queryClient: QueryClient) {
  try {
    const snapshot: Snapshot = {
      v: CACHE_VERSION,
      savedAt: Date.now(),
      owner,
      state: dehydrate(queryClient, { shouldDehydrateQuery: isPersistedQuery, shouldDehydrateMutation: () => false }),
    };
    localStorage.setItem(STORAGE_KEY, JSON.stringify(snapshot, withoutForeignMediaUrls));
  } catch {
    // Quota exceeded or storage unavailable (private mode): no offline copy.
  }
}

/** Puts the saved copy back into the client. Returns the owner it belonged to. */
export function restoreQueryCache(queryClient: QueryClient): string | null {
  const snapshot = read();
  if (!snapshot) {
    clearPersistedCache();
    return null;
  }
  owner = snapshot.owner;
  hydrate(queryClient, snapshot.state);
  return owner;
}

/** Keeps the saved copy up to date. Returns the unsubscribe function. */
export function persistQueryCache(queryClient: QueryClient): () => void {
  let pending: ReturnType<typeof setTimeout> | undefined;
  const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
    if (event.type !== "updated" && event.type !== "removed") return;
    if (!isPersistedQuery(event.query) && event.type !== "removed") return;
    if (pending) return;
    pending = setTimeout(() => {
      pending = undefined;
      write(queryClient);
    }, WRITE_THROTTLE_MS);
  });
  return () => {
    clearTimeout(pending);
    unsubscribe();
  };
}

export function clearPersistedCache(): void {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

function isViewerQuery(query: Pick<Query, "queryKey">): boolean {
  const root = query.queryKey[0];
  return typeof root === "string" && VIEWER_QUERY_ROOTS.has(root);
}

/**
 * The viewer is now known (user id, or "guest"/"open"). If the saved copy
 * belonged to someone else, drop their data from storage and from the
 * client. The viewer's own status and profile are never dropped — removing
 * them would cancel a request in flight and leave the page on the previous
 * viewer's answer — they are asked again instead. The copy written from now
 * on belongs to the new owner.
 */
export function setCacheOwner(queryClient: QueryClient, next: string): void {
  if (owner !== null && owner !== next) {
    queryClient.removeQueries({ predicate: (q) => isPersistedQuery(q) && !isViewerQuery(q) });
    clearPersistedCache();
    void queryClient.invalidateQueries({ predicate: isViewerQuery });
  }
  owner = next;
}

export function getCacheOwner(): string | null {
  return owner;
}

/** When the newest saved query was fetched (ms epoch), or null. */
export function newestSavedAt(queryClient: QueryClient): number | null {
  let newest: number | null = null;
  for (const query of queryClient.getQueryCache().getAll()) {
    if (!isPersistedQuery(query)) continue;
    const at = query.state.dataUpdatedAt;
    if (at && (newest === null || at > newest)) newest = at;
  }
  return newest;
}

/** Test helper. */
export function resetPersistenceForTests(): void {
  owner = null;
}
