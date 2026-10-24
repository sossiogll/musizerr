import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { viewerStatus } from "@/test/viewer";
import { act, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  LIVE_STATES_LIMIT,
  QueueProvider,
  RECONNECT_BASE_MS,
  useQueue,
} from "./QueueContext";

// /progress needs the stream session when auth is on: the provider must open
// the EventSource only once AuthContext has a signed-in user (set after the
// stream cookie is synced), reopen it on every new sign-in, and, when the
// server closes it, renew the session and reopen with a growing delay.
const mockUseAuth = vi.fn();
const mockRefresh = vi.fn();
const sources: FakeEventSource[] = [];

class FakeEventSource {
  static readonly CLOSED = 2;
  readyState = 1;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
}

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => mockUseAuth(),
}));

vi.mock("@/lib/api-client", () => ({
  createProgressSource: () => {
    const source = new FakeEventSource();
    sources.push(source);
    return source;
  },
  refreshAccessToken: () => mockRefresh(),
}));

const user = (id: string) => ({ id, username: id, role: "user" });

let queryClient: QueryClient;

let liveCount = 0;

function LiveCount() {
  liveCount = Object.keys(useQueue().liveStatesByDownload).length;
  return null;
}

function tree() {
  return (
    <QueryClientProvider client={queryClient}>
      <QueueProvider>
        <LiveCount />
      </QueueProvider>
    </QueryClientProvider>
  );
}

function serverCloses(source: FakeEventSource) {
  source.readyState = FakeEventSource.CLOSED;
  source.onerror?.();
}

beforeEach(() => {
  queryClient = new QueryClient();
  sources.length = 0;
  liveCount = 0;
  mockUseAuth.mockReset();
  mockRefresh.mockReset();
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("QueueProvider progress stream", () => {
  it("does not open the stream while signed out with auth on", () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("guest"), user: null });
    render(tree());
    expect(sources).toHaveLength(0);
  });

  it("does not open the stream before the auth config is known", () => {
    mockUseAuth.mockReturnValue({ authConfig: undefined, user: null });
    render(tree());
    expect(sources).toHaveLength(0);
  });

  it("opens the stream once a user is signed in", () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: user("u1") });
    render(tree());
    expect(sources).toHaveLength(1);
    expect(sources[0].closed).toBe(false);
  });

  it("opens the stream without a user when auth is off", () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("open"), user: null });
    render(tree());
    expect(sources).toHaveLength(1);
  });

  it("closes on sign-out and reopens on the next sign-in", () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: user("u1") });
    const { rerender } = render(tree());
    expect(sources).toHaveLength(1);

    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("guest"), user: null });
    rerender(tree());
    expect(sources[0].closed).toBe(true);

    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: user("u1") });
    rerender(tree());
    expect(sources).toHaveLength(2);
    expect(sources[1].closed).toBe(false);
  });

  it("renews the session and reopens after the server closes the stream", async () => {
    vi.useFakeTimers();
    mockRefresh.mockResolvedValue("new-token");
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: user("u1") });
    render(tree());

    serverCloses(sources[0]);
    expect(sources).toHaveLength(1); // non subito: dopo l'attesa

    await act(() => vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS));
    expect(mockRefresh).toHaveBeenCalledTimes(1);
    expect(sources).toHaveLength(2);
  });

  it("waits longer after each failed reopen", async () => {
    vi.useFakeTimers();
    mockRefresh.mockResolvedValue("new-token");
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: user("u1") });
    render(tree());

    serverCloses(sources[0]);
    await act(() => vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS));
    serverCloses(sources[1]);
    await act(() => vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS));
    expect(sources).toHaveLength(2);

    await act(() => vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS));
    expect(sources).toHaveLength(3);
  });

  it("does not reopen when the session cannot be renewed", async () => {
    vi.useFakeTimers();
    mockRefresh.mockResolvedValue(null);
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("user"), user: user("u1") });
    render(tree());

    serverCloses(sources[0]);
    await act(() => vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS * 100));

    expect(sources).toHaveLength(1);
  });

  it("with auth off reopens without renewing anything", async () => {
    vi.useFakeTimers();
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("open"), user: null });
    render(tree());

    serverCloses(sources[0]);
    await act(() => vi.advanceTimersByTimeAsync(RECONNECT_BASE_MS));

    expect(mockRefresh).not.toHaveBeenCalled();
    expect(sources).toHaveLength(2);
  });

  it("keeps a bounded number of live states", () => {
    mockUseAuth.mockReturnValue({ authConfig: viewerStatus("open"), user: null });
    render(tree());

    act(() => {
      for (let i = 0; i < LIVE_STATES_LIMIT + 50; i++) {
        sources[0].onmessage?.(
          new MessageEvent("message", { data: JSON.stringify({ download_id: `d${i}`, status: "downloaded" }) }),
        );
      }
    });

    expect(liveCount).toBe(LIVE_STATES_LIMIT);
  });
});
