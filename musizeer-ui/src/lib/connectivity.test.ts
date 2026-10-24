import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import axios from "axios";
import { onlineManager } from "@tanstack/react-query";
import {
  CONFIRM_DELAY_MS,
  HEARTBEAT_MS,
  OFFLINE_AFTER_FAILURES,
  RETRY_BASE_MS,
  RETRY_MAX_MS,
  backoffDelay,
  checkNow,
  getConnectivity,
  reportUnreachable,
  startConnectivityMonitor,
  stopConnectivityMonitor,
} from "./connectivity";

let healthy = true;

beforeEach(() => {
  vi.useFakeTimers();
  healthy = true;
  vi.spyOn(axios, "get").mockImplementation(() => (healthy ? Promise.resolve({ status: 200 }) : Promise.reject(new Error("down"))));
});

afterEach(() => {
  stopConnectivityMonitor();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function flush() {
  // Let the /health promise chain settle.
  await vi.advanceTimersByTimeAsync(0);
}

describe("backoff", () => {
  it("doubles from the base up to the maximum", () => {
    expect(backoffDelay(1)).toBe(RETRY_BASE_MS);
    expect(backoffDelay(2)).toBe(RETRY_BASE_MS * 2);
    expect(backoffDelay(3)).toBe(RETRY_BASE_MS * 4);
    expect(backoffDelay(50)).toBe(RETRY_MAX_MS);
  });
});

describe("connectivity monitor", () => {
  it("starts online when /health answers, and drives TanStack's onlineManager", async () => {
    startConnectivityMonitor();
    await flush();

    expect(getConnectivity().online).toBe(true);
    expect(getConnectivity().lastOnlineAt).not.toBeNull();
    expect(onlineManager.isOnline()).toBe(true);
  });

  it("an unreachable request triggers a check; consecutive failing checks go offline", async () => {
    startConnectivityMonitor();
    await flush();
    healthy = false;

    reportUnreachable();
    await flush();
    // One failure is not an outage yet: it is confirmed first.
    expect(getConnectivity().online).toBe(true);
    for (let i = 1; i < OFFLINE_AFTER_FAILURES; i++) {
      await vi.advanceTimersByTimeAsync(CONFIRM_DELAY_MS);
    }

    expect(axios.get).toHaveBeenCalledTimes(1 + OFFLINE_AFTER_FAILURES);
    expect(getConnectivity().online).toBe(false);
    expect(onlineManager.isOnline()).toBe(false);
    expect(getConnectivity().nextCheckAt).not.toBeNull();
  });

  it("one /health that times out behind stuck requests does not take the app offline", async () => {
    startConnectivityMonitor();
    await flush();
    const get = vi.mocked(axios.get);
    // The check waits for a connection and times out; the next one gets through.
    get.mockImplementationOnce(() => Promise.reject(new Error("timeout of 5000ms exceeded")));

    reportUnreachable();
    await flush();
    expect(getConnectivity().online).toBe(true);
    await vi.advanceTimersByTimeAsync(CONFIRM_DELAY_MS);

    expect(getConnectivity().online).toBe(true);
    expect(onlineManager.isOnline()).toBe(true);
    // The failure is forgotten: a later outage again needs every confirmation.
    healthy = false;
    reportUnreachable();
    await flush();
    await vi.advanceTimersByTimeAsync(CONFIRM_DELAY_MS * (OFFLINE_AFTER_FAILURES - 2));
    expect(getConnectivity().online).toBe(true);
    await vi.advanceTimersByTimeAsync(CONFIRM_DELAY_MS);
    expect(getConnectivity().online).toBe(false);
  });

  it("polls with backoff while offline and recovers on its own", async () => {
    healthy = false;
    startConnectivityMonitor();
    await flush();
    const get = vi.mocked(axios.get);
    await vi.advanceTimersByTimeAsync(CONFIRM_DELAY_MS * (OFFLINE_AFTER_FAILURES - 1));
    expect(get).toHaveBeenCalledTimes(OFFLINE_AFTER_FAILURES);
    expect(getConnectivity().online).toBe(false);

    await vi.advanceTimersByTimeAsync(RETRY_BASE_MS);
    expect(get).toHaveBeenCalledTimes(OFFLINE_AFTER_FAILURES + 1);
    // Next failure: the next attempt waits twice as long.
    await vi.advanceTimersByTimeAsync(RETRY_BASE_MS);
    expect(get).toHaveBeenCalledTimes(OFFLINE_AFTER_FAILURES + 1);
    healthy = true;
    await vi.advanceTimersByTimeAsync(RETRY_BASE_MS);
    expect(get).toHaveBeenCalledTimes(OFFLINE_AFTER_FAILURES + 2);

    expect(getConnectivity().online).toBe(true);
    expect(onlineManager.isOnline()).toBe(true);
  });

  it("while online, a slow heartbeat notices an idle outage", async () => {
    startConnectivityMonitor();
    await flush();
    healthy = false;

    await vi.advanceTimersByTimeAsync(HEARTBEAT_MS);
    expect(getConnectivity().online).toBe(true);
    await vi.advanceTimersByTimeAsync(CONFIRM_DELAY_MS * (OFFLINE_AFTER_FAILURES - 1));

    expect(getConnectivity().online).toBe(false);
  });

  it("one check at a time", async () => {
    startConnectivityMonitor();
    await flush();
    vi.mocked(axios.get).mockClear();

    await Promise.all([checkNow(), checkNow(), checkNow()]);

    expect(axios.get).toHaveBeenCalledTimes(1);
  });

  it("the browser going offline is taken at its word until /health says otherwise", async () => {
    startConnectivityMonitor();
    await flush();

    window.dispatchEvent(new Event("offline"));
    expect(getConnectivity().online).toBe(false);

    window.dispatchEvent(new Event("online"));
    await flush();
    expect(getConnectivity().online).toBe(true);
  });

  it("before the monitor starts, reports are ignored", () => {
    reportUnreachable();
    expect(axios.get).not.toHaveBeenCalled();
  });
});
