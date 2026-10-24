import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nProvider } from "@/i18n";
import { PlayerProvider, usePlayer } from "@/contexts/PlayerContext";
import { AdminDuplicatesPage } from "./duplicates";
import type { DuplicateGroup, DuplicateMember } from "@/lib/api-client";

vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const mockGet = vi.fn();
const mockResolve = vi.fn();
const mockDismiss = vi.fn();
const mockResolveAll = vi.fn();
vi.mock("@/lib/api-client", () => ({
  getDuplicateGroups: (...a: unknown[]) => mockGet(...a),
  resolveDuplicateGroup: (...a: unknown[]) => mockResolve(...a),
  dismissDuplicateGroup: (...a: unknown[]) => mockDismiss(...a),
  resolveAllDuplicateGroups: () => mockResolveAll(),
  getPublicProviders: async () => ({ providers: [{ id: "dl", label: "Downloader" }], metadata: [], metadata_priority: [], metadata_capabilities: {} }),
}));

const member = (id: string, over: Partial<DuplicateMember>): DuplicateMember => ({
  id, sync_id: id, path: `A/${id}`, title: "Song", artist: "Artist", album: "Album", year: 2020,
  duration_ms: 200_000, size: 5_000_000, suffix: "flac", bitrate: 1_000_000, sample_rate: 44100, lossless: true,
  quality_known: true, fingerprint_score: 0.97, origin: "musizeer", deletable: true, is_keeper: false, deleted_at: null,
  storage_path: null, added_at: null, downloaded_via: null, best_quality: false, stream_url: `/api/library/duplicates/members/${id}/stream`,
  ...over,
});

const group: DuplicateGroup = {
  id: "g1", recording_id: "rec", status: "proposed", reason: null, created_at: "", updated_at: "", resolved_at: null,
  deleted_paths: [],
  members: [
    member("best.flac", { is_keeper: true }),
    member("worse.mp3", { suffix: "mp3", lossless: false, bitrate: 320_000 }),
    member("external.mp3", { origin: "unknown", deletable: false }),
  ],
};

/** What the player holds, shown next to the page. */
function NowPlaying() {
  const { track, interlude, queue } = usePlayer();
  return (
    <output aria-label="now playing">
      {track ? `${track.stream_url} | ${track.detached?.note} | ${interlude ? "alone" : "queue"} | ${queue.length}` : "nothing"}
    </output>
  );
}

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <I18nProvider>
        <PlayerProvider>
          <AdminDuplicatesPage />
          <NowPlaying />
        </PlayerProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.HTMLMediaElement.prototype.play = vi.fn(function (this: HTMLMediaElement) {
    this.dispatchEvent(new Event("play"));
    return Promise.resolve();
  });
  window.HTMLMediaElement.prototype.pause = vi.fn();
  mockGet.mockResolvedValue([group]);
  mockResolve.mockResolvedValue({ group_id: "g1", status: "resolved", deleted: [], skipped_non_deletable: [] });
  mockDismiss.mockResolvedValue({ group_id: "g1", status: "dismissed" });
  mockResolveAll.mockResolvedValue({ resolved: [] });
});

describe("Administration › Duplicates", () => {
  it("lists proposed groups with the keeper preselected and the origin of each file", async () => {
    renderPage();
    const radios = await screen.findAllByRole("radio");
    expect(mockGet).toHaveBeenCalledWith("proposed");
    expect(radios[0]).toBeChecked();
    expect(screen.getByText("Added outside Musizeer")).toBeInTheDocument();
    expect(screen.getAllByText("Downloaded by Musizeer")).toHaveLength(2);
  });

  it("resolving asks first and names only the files Musizeer can delete", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Keep selected, delete the others" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Delete 1 file?");
    expect(dialog).toHaveTextContent("A/worse.mp3");
    expect(dialog).not.toHaveTextContent("A/external.mp3");
    expect(mockResolve).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Keep selected, delete the others" }));
    await waitFor(() => expect(mockResolve).toHaveBeenCalledWith("g1", "best.flac"));
  });

  it("keeping another file changes what would be deleted", async () => {
    renderPage();
    const radios = await screen.findAllByRole("radio");
    fireEvent.click(radios[1]);
    fireEvent.click(screen.getByRole("button", { name: "Keep selected, delete the others" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("A/best.flac");
  });

  it("'not a duplicate' dismisses the group", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Not a duplicate" }));
    await waitFor(() => expect(mockDismiss).toHaveBeenCalledWith("g1"));
  });

  it("resolve all asks first", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Resolve all proposed" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Resolve all proposed" }));
    await waitFor(() => expect(mockResolveAll).toHaveBeenCalled());
  });

  it("a group where nothing can be deleted offers 'mark as handled', never a deletion", async () => {
    // Two files the sync provider reports under ONE virtual path, both added
    // outside Musizeer: the old page asked "Delete 0 files?".
    mockGet.mockResolvedValue([{
      ...group, status: "needs_review",
      members: [
        member("sid-1", { path: "A/01-04 - La fine.mp3", origin: "unknown", deletable: false, is_keeper: false }),
        member("sid-2", { path: "A/01-04 - La fine.mp3", origin: "unknown", deletable: false, is_keeper: false }),
      ],
    }]);
    renderPage();

    expect(await screen.findByText(/nothing can be deleted from here/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Keep selected, delete the others" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Not a duplicate" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mark as handled" }));

    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(mockResolve).toHaveBeenCalledWith("g1", "sid-1"));
  });

  it("keeping the only file Musizeer downloaded deletes nothing: the action says so", async () => {
    mockGet.mockResolvedValue([{
      ...group,
      members: [member("ours.flac", { is_keeper: true }), member("external.mp3", { origin: "unknown", deletable: false })],
    }]);
    renderPage();

    expect(await screen.findByRole("button", { name: "Mark as handled" })).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("radio")[1]);
    expect(screen.getByRole("button", { name: "Keep selected, delete the others" })).toBeInTheDocument();
  });

  it("filters by status; closed groups have no actions", async () => {
    mockGet.mockImplementation(async (status: string) => (status === "resolved" ? [{ ...group, status: "resolved", deleted_paths: ["A/worse.mp3"] }] : []));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Resolved" }));

    expect(await screen.findByText("Deleted: A/worse.mp3")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Not a duplicate" })).not.toBeInTheDocument();
  });

  it("a group without a keeper preselects nothing: the 128 kbps external file is not chosen for you", async () => {
    // "Italiano a Barcelona": an external 128 kbps MP3 listed first, the
    // 332 kbps OGG Musizeer downloaded second. Preselecting the first one
    // made the primary button delete the better file.
    mockGet.mockResolvedValue([{
      ...group, status: "needs_review", reason: "external_or_unknown_origin_would_be_deleted",
      members: [
        member("ext", { suffix: "mp3", lossless: false, bitrate: 128_000, origin: "unknown", deletable: false }),
        member("ours", { suffix: "ogg", lossless: false, bitrate: 332_000, best_quality: true, storage_path: "Real/Ours.ogg" }),
      ],
    }]);
    renderPage();

    const radios = await screen.findAllByRole("radio");
    expect(radios.every((radio) => !(radio as HTMLInputElement).checked)).toBe(true);
    expect(screen.getByText(/No file is chosen for you here/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep selected, delete the others" })).toBeDisabled();
    // The better file is pointed out, with why; the decision stays the admin's.
    expect(screen.getByText("Best quality")).toBeInTheDocument();
    expect(screen.getByText("Why: Would delete a file added outside Musizeer")).toBeInTheDocument();

    fireEvent.click(radios[0]); // keep the external one: deletes ours, so it asks first
    fireEvent.click(screen.getByRole("button", { name: "Keep selected, delete the others" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Real/Ours.ogg");
    expect(mockResolve).not.toHaveBeenCalled();
  });

  it("reasons are translated with an explanation, never a raw code (it)", async () => {
    localStorage.setItem("musizeer-lang", "it");
    mockGet.mockResolvedValue([{ ...group, status: "needs_review", reason: "unknown_quality" }]);
    renderPage();
    expect(await screen.findByText("Motivo: Qualità sconosciuta")).toBeInTheDocument();
    expect(screen.getByText(/non si possono confrontare con certezza/)).toBeInTheDocument();
    expect(screen.queryByText(/unknown_quality/)).not.toBeInTheDocument();
  });

  it("shows the real storage path and what tells two copies with one virtual path apart", async () => {
    mockGet.mockResolvedValue([{
      ...group, status: "needs_review",
      members: [
        member("a", { path: "L'Amour Toujours.mp3", storage_path: "Gigi D'Agostino/2000/L'Amour Toujours.mp3", suffix: "mp3", lossless: false, bitrate: 320_000, added_at: "2026-09-01T10:00:00Z", downloaded_via: "dl" }),
        member("b", { path: "L'Amour Toujours.mp3", storage_path: "Gigi D'Agostino/1999/L'Amour Toujours.mp3", suffix: "mp3", lossless: false, bitrate: 192_000 }),
      ],
    }]);
    renderPage();

    expect(await screen.findByText("Gigi D'Agostino/2000/L'Amour Toujours.mp3")).toBeInTheDocument();
    expect(screen.getByText("Gigi D'Agostino/1999/L'Amour Toujours.mp3")).toBeInTheDocument();
    expect(screen.getAllByText("Sync server path: L'Amour Toujours.mp3")).toHaveLength(2);
    expect(screen.getByText(/MP3 · 320 kbps/)).toBeInTheDocument();
    expect(screen.getByText(/MP3 · 192 kbps/)).toBeInTheDocument();
    expect(await screen.findByText("Downloaded by Musizeer via Downloader")).toBeInTheDocument();
    expect(screen.getByText(/^Added /)).toBeInTheDocument();
  });

  it("each file plays its own stream on its own, and the player says which file it is", async () => {
    renderPage();
    const play = await screen.findByRole("button", { name: "Play this file: A/worse.mp3" });
    expect(play).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(play);

    const now = screen.getByRole("status", { name: "now playing" });
    expect(now).toHaveTextContent("/api/library/duplicates/members/worse.mp3/stream");
    expect(now).toHaveTextContent("Duplicate file · MP3 · 320 kbps");
    // Heard on its own: no queue was made of it.
    expect(now).toHaveTextContent("alone | 0");
    await waitFor(() => expect(screen.getByRole("button", { name: "Pause this file: A/worse.mp3" })).toHaveAttribute("aria-pressed", "true"));
  });

  it("a deleted file has nothing to play", async () => {
    mockGet.mockResolvedValue([{ ...group, members: [member("gone", { deleted_at: "2026-09-01T00:00:00Z", stream_url: null }), member("kept", { is_keeper: true })] }]);
    renderPage();
    await screen.findByRole("button", { name: "Play this file: A/kept" });
    expect(screen.queryByRole("button", { name: "Play this file: A/gone" })).not.toBeInTheDocument();
  });
});
