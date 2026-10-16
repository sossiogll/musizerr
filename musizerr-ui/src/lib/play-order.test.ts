import { beforeEach, describe, expect, it } from "vitest";
import {
  PLAY_MODE_STORAGE_KEY,
  candidates,
  listOrder,
  nextRepeatMode,
  playOrder,
  readPlayMode,
  shuffledOrder,
  writePlayMode,
} from "./play-order";

/** A seeded generator: the same "random" order on every run. */
function seeded(seed = 7) {
  let state = seed;
  return () => {
    state = (state * 16807) % 2147483647;
    return (state - 1) / 2147483646;
  };
}

describe("shuffledOrder", () => {
  it("keeps the current track first and plays every other one exactly once", () => {
    const order = shuffledOrder(10, 4, seeded());
    expect(order[0]).toBe(4);
    expect([...order].sort((a, b) => a - b)).toEqual(listOrder(10));
  });

  it("is a real shuffle, not the list order", () => {
    expect(shuffledOrder(10, 0, seeded())).not.toEqual(listOrder(10));
  });

  it("with nothing playing (-1) shuffles the whole queue", () => {
    const order = shuffledOrder(5, -1, seeded());
    expect([...order].sort()).toEqual(listOrder(5));
  });

  it("an empty or one-track queue", () => {
    expect(shuffledOrder(0, -1)).toEqual([]);
    expect(shuffledOrder(1, 0)).toEqual([0]);
  });
});

describe("playOrder", () => {
  it("back to the list order when shuffle is off, whatever plays", () => {
    expect(playOrder(4, 2, false)).toEqual([0, 1, 2, 3]);
    expect(playOrder(4, 2, true, seeded())[0]).toBe(2);
  });
});

describe("candidates (what next/previous try, in turn)", () => {
  const order = [2, 0, 3, 1];

  it("next and previous follow the play order, not the list", () => {
    expect(candidates(order, 0, 1, "off")).toEqual([3, 1]);
    expect(candidates(order, 0, -1, "off")).toEqual([2]);
  });

  it("repeat off: nothing after the last, nothing before the first", () => {
    expect(candidates(order, 1, 1, "off")).toEqual([]);
    expect(candidates(order, 2, -1, "off")).toEqual([]);
  });

  it("repeat all: around from the other end, never the current track again", () => {
    expect(candidates(order, 1, 1, "all")).toEqual([2, 0, 3]);
    expect(candidates(order, 2, -1, "all")).toEqual([1, 3, 0]);
  });

  it("repeat one: a skip moves on as with all (only the end of a track repeats it)", () => {
    expect(candidates(order, 1, 1, "one")).toEqual([2, 0, 3]);
  });

  it("a track not in the order (nothing playing) has no candidates", () => {
    expect(candidates(order, -1, 1, "all")).toEqual([]);
  });
});

describe("repeat modes", () => {
  it("cycle off → all → one → off", () => {
    expect(nextRepeatMode("off")).toBe("all");
    expect(nextRepeatMode("all")).toBe("one");
    expect(nextRepeatMode("one")).toBe("off");
  });
});

describe("play mode on this device", () => {
  beforeEach(() => localStorage.clear());

  it("defaults to no shuffle, no repeat", () => {
    expect(readPlayMode()).toEqual({ shuffle: false, repeat: "off" });
  });

  it("remembers what was set", () => {
    writePlayMode({ shuffle: true, repeat: "one" });
    expect(readPlayMode()).toEqual({ shuffle: true, repeat: "one" });
  });

  it("ignores a broken or foreign value", () => {
    localStorage.setItem(PLAY_MODE_STORAGE_KEY, "{not json");
    expect(readPlayMode()).toEqual({ shuffle: false, repeat: "off" });
    localStorage.setItem(PLAY_MODE_STORAGE_KEY, JSON.stringify({ shuffle: "yes", repeat: "forever" }));
    expect(readPlayMode()).toEqual({ shuffle: false, repeat: "off" });
  });
});
