import { vi } from "vitest";

type Listener = () => void;

interface Entry {
  matches: boolean;
  listeners: Set<Listener>;
}

/**
 * A controllable `window.matchMedia`: each query matches what `initial` says
 * (default: no), `set(query, matches)` changes the answer and tells the
 * listeners, `listeners(query)` counts the ones still attached, `asked` lists
 * every query that was evaluated. Restored by `restore()`.
 */
export function installMatchMedia(initial: Record<string, boolean> = {}) {
  const original = window.matchMedia;
  const entries = new Map<string, Entry>();
  const asked: string[] = [];

  const entry = (query: string): Entry => {
    let e = entries.get(query);
    if (!e) {
      e = { matches: initial[query] ?? false, listeners: new Set() };
      entries.set(query, e);
    }
    return e;
  };

  window.matchMedia = vi.fn((query: string) => {
    asked.push(query);
    const e = entry(query);
    return {
      get matches() {
        return e.matches;
      },
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: (_: string, l: Listener) => e.listeners.add(l),
      removeEventListener: (_: string, l: Listener) => e.listeners.delete(l),
      dispatchEvent: () => false,
    };
  }) as unknown as typeof window.matchMedia;

  return {
    asked,
    set(query: string, matches: boolean) {
      const e = entry(query);
      e.matches = matches;
      e.listeners.forEach((l) => l());
    },
    listeners: (query: string) => entry(query).listeners.size,
    restore() {
      window.matchMedia = original;
    },
  };
}
