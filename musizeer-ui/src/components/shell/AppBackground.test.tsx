import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { installMatchMedia } from "@/test/media";
import { BACKGROUND_DESKTOP_QUERY } from "@/lib/backgrounds";
import { AppBackground } from "./AppBackground";

// The gate in front of the animated background: whatever the setting and the
// device say, the canvas code is only fetched (and so only runs) for a signed-in
// viewer on a desktop who chose one.
const { loads, mockSignedIn, mockPlayer, mockValues } = vi.hoisted(() => ({
  loads: { count: 0 },
  mockSignedIn: vi.fn(),
  mockPlayer: vi.fn(),
  mockValues: vi.fn(),
}));

vi.mock("@/components/background/BackgroundCanvas", () => {
  loads.count += 1; // the factory runs once, when the module is first imported
  return {
    default: (props: { kind: string; playing: boolean; title: string | null; artist: string | null }) => (
      <div data-testid="canvas" data-kind={props.kind} data-playing={String(props.playing)}>
        {props.title}|{props.artist}
      </div>
    ),
  };
});
vi.mock("@/contexts/PreferencesSync", () => ({ useIsSignedIn: () => mockSignedIn() }));
vi.mock("@/contexts/PlayerContext", () => ({ usePlayer: () => mockPlayer() }));
vi.mock("@/lib/api-client", () => ({ getSettingsValues: () => mockValues() }));

function renderBackground() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <AppBackground />
    </QueryClientProvider>,
  );
}

let media: ReturnType<typeof installMatchMedia>;

beforeEach(() => {
  media = installMatchMedia({ [BACKGROUND_DESKTOP_QUERY]: true });
  mockSignedIn.mockReturnValue(true);
  mockPlayer.mockReturnValue({ track: null, playing: false });
  mockValues.mockResolvedValue({ background: "aurora" });
});

afterEach(() => {
  media.restore();
  vi.clearAllMocks();
});

// vi.mock factories are evaluated lazily and once per test file: the module
// is never imported by the tests above this one, so the count tells.
describe("AppBackground", () => {
  it("fetches nothing of the canvas while the setting is off", async () => {
    mockValues.mockResolvedValue({ background: "off" });
    const { container } = renderBackground();
    await waitFor(() => expect(mockValues).toHaveBeenCalledTimes(1));
    expect(container).toBeEmptyDOMElement();
    expect(loads.count).toBe(0);
  });

  it("is off when the viewer never chose (the key is absent) or the value is unknown", async () => {
    mockValues.mockResolvedValue({});
    const first = renderBackground();
    await waitFor(() => expect(mockValues).toHaveBeenCalledTimes(1));
    expect(first.container).toBeEmptyDOMElement();
    first.unmount();

    mockValues.mockResolvedValue({ background: "confetti" });
    const second = renderBackground();
    await waitFor(() => expect(mockValues).toHaveBeenCalledTimes(2));
    expect(second.container).toBeEmptyDOMElement();
    expect(loads.count).toBe(0);
  });

  it("mounts nothing on a device that is not a desktop, and never fetches the canvas code", async () => {
    media.set(BACKGROUND_DESKTOP_QUERY, false);
    const { container } = renderBackground();
    await waitFor(() => expect(mockValues).toHaveBeenCalledTimes(1));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByTestId("canvas")).toBeNull();
    expect(loads.count).toBe(0);
  });

  it("does not read the settings at all without an account", async () => {
    mockSignedIn.mockReturnValue(false);
    const { container } = renderBackground();
    await act(async () => {});
    expect(mockValues).not.toHaveBeenCalled();
    expect(container).toBeEmptyDOMElement();
    expect(loads.count).toBe(0);
  });

  // From here on the canvas module is imported (once, for the whole file).
  it("asks the desktop media query and nothing else", async () => {
    renderBackground();
    await screen.findByTestId("canvas");
    expect(new Set(media.asked)).toEqual(new Set([BACKGROUND_DESKTOP_QUERY]));
    expect(media.listeners(BACKGROUND_DESKTOP_QUERY)).toBe(1);
  });

  it.each(["aurora", "waves", "spectrum", "typography"])("lazy-loads and shows the %s background", async (kind) => {
    mockValues.mockResolvedValue({ background: kind });
    renderBackground();
    expect(await screen.findByTestId("canvas")).toHaveAttribute("data-kind", kind);
  });

  it("imports the canvas module exactly once, however often it renders", async () => {
    const { rerender } = renderBackground();
    await screen.findByTestId("canvas");
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <AppBackground />
      </QueryClientProvider>,
    );
    expect(loads.count).toBe(1);
  });

  it("follows the media query: gone when the window narrows, back when it widens", async () => {
    renderBackground();
    await screen.findByTestId("canvas");
    act(() => media.set(BACKGROUND_DESKTOP_QUERY, false));
    expect(screen.queryByTestId("canvas")).toBeNull();
    act(() => media.set(BACKGROUND_DESKTOP_QUERY, true));
    expect(await screen.findByTestId("canvas")).toBeInTheDocument();
  });

  it("removes its media query listener on unmount", async () => {
    const { unmount } = renderBackground();
    await screen.findByTestId("canvas");
    unmount();
    expect(media.listeners(BACKGROUND_DESKTOP_QUERY)).toBe(0);
  });

  it("hands the canvas what is playing", async () => {
    mockValues.mockResolvedValue({ background: "typography" });
    mockPlayer.mockReturnValue({ track: { title: "Song", artist: "Band" }, playing: true });
    renderBackground();
    const canvas = await screen.findByTestId("canvas");
    expect(canvas).toHaveAttribute("data-playing", "true");
    expect(canvas).toHaveTextContent("Song|Band");
  });
});
