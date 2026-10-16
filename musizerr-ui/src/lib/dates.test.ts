import { describe, expect, it } from "vitest";
import { formatDuration, parseApiDate, relativeTime } from "./dates";

// vitest.config.ts runs the suite in Europe/Rome (UTC+2 on these dates).
describe("parseApiDate", () => {
  it("keeps the offset the API sends", () => {
    expect(parseApiDate("2026-09-29T09:38:06.018415+00:00").toISOString()).toBe("2026-09-29T09:38:06.018Z");
    expect(parseApiDate("2026-09-29T09:38:06Z").toISOString()).toBe("2026-09-29T09:38:06.000Z");
    expect(parseApiDate("2026-09-29T11:38:06+02:00").toISOString()).toBe("2026-09-29T09:38:06.000Z");
  });

  it("reads a datetime without an offset as UTC, never as local time", () => {
    expect(parseApiDate("2026-09-29T09:38:06.018415").toISOString()).toBe("2026-09-29T09:38:06.018Z");
  });

  it("leaves a plain date alone", () => {
    expect(parseApiDate("2026-09-29").toISOString()).toBe("2026-09-29T00:00:00.000Z");
  });
});

describe("relativeTime / formatDuration", () => {
  const now = Date.parse("2026-09-29T12:35:57Z");

  it("counts from the same clock whatever the zone", () => {
    expect(relativeTime("2026-09-29T12:32:57", "en", now)).toBe("3 minutes ago");
    expect(relativeTime("2026-09-29T13:32:57+00:00", "en", now)).toBe("in 57 minutes");
  });

  it("says a duration in the largest units that fit", () => {
    expect(formatDuration(0.034, "en")).toBe("0 s");
    expect(formatDuration(1.1, "en")).toBe("1.1 s");
    expect(formatDuration(42, "en")).toBe("42 s");
    expect(formatDuration(185, "en")).toBe("3 min 5 s");
    expect(formatDuration(7800, "en")).toBe("2 h 10 min");
  });
});
