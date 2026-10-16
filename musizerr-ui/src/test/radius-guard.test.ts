/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";

/**
 * Corners are rounded rectangles on one scale (xs–xl, derived from --radius
 * in index.css). A pill or a circle is allowed only where the content is
 * round by nature; each such use is listed here with its reason, so a new
 * `rounded-full` fails until someone decides it belongs on this list.
 */
const ROUND_BY_NATURE: Record<string, { count: number; reason: string }> = {
  "src/components/CoverImage.tsx": { count: 1, reason: "artist image" },
  "src/routes/artist.tsx": { count: 1, reason: "artist image skeleton" },
  "src/components/shell/AccountMenu.tsx": { count: 1, reason: "user avatar" },
  "src/routes/admin/users.tsx": { count: 1, reason: "user avatar" },
  "src/components/shell/ServiceStatus.tsx": { count: 1, reason: "status dot" },
  "src/routes/admin/providers.tsx": { count: 1, reason: "status dot" },
  "src/components/tracks/FavoriteHeart.tsx": { count: 1, reason: "dot behind the round clock of a pending heart" },
};

const sources = import.meta.glob<string>(["/src/**/*.{ts,tsx,css}", "!/src/**/*.test.{ts,tsx}", "!/src/test/**"], {
  query: "?raw",
  import: "default",
  eager: true,
});
const files = Object.entries(sources).map(([path, text]) => ({ path: path.slice(1), text }));

function offending(pattern: RegExp): string[] {
  return files.flatMap(({ path, text }) =>
    text.split("\n").flatMap((line, i) => (pattern.test(line) ? [`${path}:${i + 1}: ${line.trim()}`] : [])),
  );
}

describe("corner radius guard", () => {
  it("scans the sources", () => {
    expect(files.length).toBeGreaterThan(50);
  });

  it("rounded-full only where the content is round (avatars, artist images, dots)", () => {
    const counts = Object.fromEntries(files.map(({ path, text }) => [path, text.match(/\brounded-full\b/g)?.length ?? 0]));
    const unexpected = Object.entries(counts)
      .filter(([path, n]) => n !== (ROUND_BY_NATURE[path]?.count ?? 0))
      .map(([path, n]) => `${path}: ${n} rounded-full, ${ROUND_BY_NATURE[path]?.count ?? 0} allowed`);
    expect(unexpected).toEqual([]);
  });

  it("no radius outside the scale: no arbitrary values, no 2xl–4xl", () => {
    expect(offending(/\brounded(-[trblse]{1,2})?-\[(?!inherit\])/)).toEqual([]);
    expect(offending(/\brounded(-[trblse]{1,2})?-[234]xl\b/)).toEqual([]);
  });

  it("stylesheets use the radius tokens, not pill values", () => {
    const css = files.filter(({ path }) => path.endsWith(".css"));
    const pills = css.flatMap(({ path, text }) => (/border-radius:\s*(9{3,}px|50%)/.test(text) ? [path] : []));
    expect(pills).toEqual([]);
  });
});
