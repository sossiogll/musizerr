import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders } from "axios";
import { I18nProvider } from "@/i18n";
import { connectivityMock } from "@/test/connectivity-mock";
import { makeTrack } from "@/test/fixtures";
import { viewerStatus } from "@/test/viewer";
import { ActionMenu } from "@/components/menu/ActionMenu";
import { trackRef, useAddToPlaylist } from "./AddToPlaylist";
import type { Track, UserPlaylist } from "@/types";

// The REAL menu (Radix), the "Add to playlist" submenu opened from the
// keyboard: the playlists are asked for when it opens, only the viewer's own
// can take a track, and the track goes to the server by the id it knows,
// else by its provider's.

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("user"), user: null }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast }));

const mockList = vi.fn();
const mockAdd = vi.fn();
const mockCreate = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getUserPlaylists: () => mockList(),
  addPlaylistTracks: (...a: unknown[]) => mockAdd(...a),
  createUserPlaylist: (...a: unknown[]) => mockCreate(...a),
}));

const playlist = (id: string, name: string, kind: "own" | "followed" = "own", count = 0): UserPlaylist => ({
  id, name, description: "", kind, source_provider: kind === "followed" ? "prov" : null, source_id: kind === "followed" ? "s" : null,
  cover_url: null, track_count: count, auto_download: false, tracks_unavailable: null, refresh_error_code: null,
  checked_at: null, created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", synced_to: [],
});

function Harness({ track }: { track: Track }) {
  const add = useAddToPlaylist(track);
  return (
    <>
      <ActionMenu entries={[{ type: "submenu", id: "playlist", label: "Add to playlist", content: add.content }]} label="Menu of Song A">
        {({ button }) => button}
      </ActionMenu>
      {add.dialog}
    </>
  );
}

function renderMenu(track = makeTrack({ title: "Song A", provider: "prov", provider_id: "p1", library_id: null })) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider>
        <Harness track={track} />
      </I18nProvider>
    </QueryClientProvider>,
  );
}

/** The menu, then its "Add to playlist" submenu, from the keyboard. */
async function open() {
  fireEvent.click(screen.getByRole("button", { name: "Menu of Song A" }));
  const sub = await screen.findByRole("menuitem", { name: "Add to playlist" });
  sub.focus();
  fireEvent.keyDown(sub, { key: "ArrowRight" });
  return screen.findByRole("menu", { name: "Add to playlist" });
}

beforeEach(() => {
  connectivityMock.reset();
  Object.values(toast).forEach((f) => f.mockReset());
  mockList.mockReset().mockResolvedValue({
    items: [playlist("a", "Road trip", "own", 3), playlist("f", "Mix from a provider", "followed", 9), playlist("b", "Gym", "own", 0)],
    max_tracks: 500,
  });
  mockAdd.mockReset().mockResolvedValue({ added: 1, skipped_duplicates: 0, playlist: playlist("a", "Road trip") });
  mockCreate.mockReset().mockResolvedValue(playlist("n", "Fresh"));
});

describe("Add to playlist", () => {
  it("asks for the playlists only when its submenu opens, not with the menu", async () => {
    renderMenu();
    fireEvent.click(screen.getByRole("button", { name: "Menu of Song A" }));
    await screen.findByRole("menuitem", { name: "Add to playlist" });
    expect(mockList).not.toHaveBeenCalled();
  });

  it("lists only the viewer's own playlists, with how many tracks each has", async () => {
    renderMenu();
    const submenu = await open();

    await waitFor(() =>
      expect(within(submenu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["Road trip3", "Gym0", "New playlist…"]),
    );
    expect(mockList).toHaveBeenCalledTimes(1);
  });

  it("adds a track the provider names by its provider and id", async () => {
    renderMenu();
    await open();
    fireEvent.keyDown(await screen.findByRole("menuitem", { name: /Road trip/ }), { key: "Enter" });

    await waitFor(() => expect(mockAdd).toHaveBeenCalledTimes(1));
    expect(mockAdd).toHaveBeenCalledWith("a", [{ provider: "prov", provider_id: "p1" }]);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Added Song A to Road trip"));
  });

  it("adds a track of the library by its library id", async () => {
    renderMenu(makeTrack({ title: "Song A", library_id: "lib-1", in_library: true }));
    await open();
    fireEvent.keyDown(await screen.findByRole("menuitem", { name: /Gym/ }), { key: "Enter" });

    await waitFor(() => expect(mockAdd).toHaveBeenCalledWith("b", [{ track_id: "lib-1" }]));
  });

  it("says when the track was already there", async () => {
    mockAdd.mockResolvedValue({ added: 0, skipped_duplicates: 1, playlist: playlist("a", "Road trip") });
    renderMenu();
    await open();
    fireEvent.keyDown(await screen.findByRole("menuitem", { name: /Road trip/ }), { key: "Enter" });

    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("Song A is already in Road trip"));
  });

  it("creates a new playlist and puts the track in it", async () => {
    renderMenu();
    await open();
    fireEvent.keyDown(await screen.findByRole("menuitem", { name: "New playlist…" }), { key: "Enter" });

    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "  Fresh  " } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(mockCreate).toHaveBeenCalledWith("Fresh"));
    await waitFor(() => expect(mockAdd).toHaveBeenCalledWith("n", [{ provider: "prov", provider_id: "p1" }]));
  });

  it("does not create a playlist without a name", async () => {
    renderMenu();
    await open();
    fireEvent.keyDown(await screen.findByRole("menuitem", { name: "New playlist…" }), { key: "Enter" });

    await screen.findByLabelText("Name");
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("tells the cap in numbers when the playlist is full", async () => {
    mockAdd.mockRejectedValue(
      new AxiosError("Conflict", "ERR_BAD_REQUEST", undefined, undefined, {
        status: 409,
        statusText: "Conflict",
        headers: {},
        config: { headers: new AxiosHeaders() },
        data: { detail: { error_code: "playlist_full", error_message: "full", max_tracks: 3, track_count: 3, adding: 1 } },
      }),
    );
    renderMenu();
    await open();
    fireEvent.keyDown(await screen.findByRole("menuitem", { name: /Road trip/ }), { key: "Enter" });

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("A playlist holds at most 3 tracks."));
  });
});

describe("trackRef", () => {
  it("prefers the id the host knows", () => {
    expect(trackRef({ library_id: "lib-1", provider: "prov", provider_id: "p1" })).toEqual({ track_id: "lib-1" });
    expect(trackRef({ library_id: null, provider: "prov", provider_id: "p1" })).toEqual({ provider: "prov", provider_id: "p1" });
  });
});
