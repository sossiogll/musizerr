/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { currentLine, hasLyrics } from "./Lyrics";

describe("currentLine", () => {
  const times = [0, 10_000, 20_000];
  it("finds the last line whose time has come", () => {
    expect(currentLine(times, -1)).toBe(-1);
    expect(currentLine(times, 0)).toBe(0);
    expect(currentLine(times, 9_999)).toBe(0);
    expect(currentLine(times, 10_000)).toBe(1);
    expect(currentLine(times, 99_000)).toBe(2);
    expect(currentLine([], 5)).toBe(-1);
  });
});

describe("hasLyrics", () => {
  it("is true for synced or plain lyrics only", () => {
    expect(hasLyrics(undefined)).toBe(false);
    expect(hasLyrics({ plain: null, synced: [], source: "x" })).toBe(false);
    expect(hasLyrics({ plain: "la la", synced: [], source: "x" })).toBe(true);
    expect(hasLyrics({ plain: null, synced: [{ time_ms: 0, text: "a" }], source: "x" })).toBe(true);
  });
});

describe("one lyrics component", () => {
  const sources = import.meta.glob<string>(["/src/routes/track.tsx", "/src/components/Player.tsx"], { query: "?raw", import: "default", eager: true });

  it("the track page and the player's panel both render LyricsBody; neither keeps its own sync logic", () => {
    expect(Object.keys(sources)).toHaveLength(2);
    for (const [path, text] of Object.entries(sources)) {
      expect(text, path).toMatch(/import \{[^}]*\bLyricsBody\b[^}]*\} from "@\/components\/lyrics\/Lyrics"/);
      expect(text, path).toContain("<LyricsBody");
      expect(text, path).not.toMatch(/function (currentLine|FollowedLyrics|LyricLine)\b/);
    }
  });
});
