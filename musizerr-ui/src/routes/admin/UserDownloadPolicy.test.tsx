import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { LOCALE_STORAGE_KEY } from "@/i18n";
import { connectivityMock } from "@/test/connectivity-mock";
import { customizedUserPolicy, generalPolicy, untouchedUserPolicy } from "@/test/download-requests";
import { renderPage } from "@/test/render";
import { UserDownloadPolicyPanel } from "./UserDownloadPolicy";
import type { User, UserDownloadPolicyView } from "@/types";

vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
const mockToast = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const mockView = vi.fn();
const mockSet = vi.fn();
const mockClear = vi.fn();
vi.mock("@/lib/api-client", () => ({
  getUserDownloadPolicy: (...a: unknown[]) => mockView(...a),
  getDownloadPolicy: async () => generalPolicy,
  setUserDownloadPolicy: (...a: unknown[]) => mockSet(...a),
  clearUserDownloadPolicy: (...a: unknown[]) => mockClear(...a),
}));

const alice: User = { id: "user-alice", username: "alice", email: null, role: "user", is_active: true };
const renderPanel = () => renderPage(() => <UserDownloadPolicyPanel user={alice} />, { path: "/admin/users" });

const mode = (kind: string) => screen.getByRole("combobox", { name: `Rule for ${kind} requests` }) as HTMLSelectElement;
const threshold = (kind: string) => screen.queryByRole("spinbutton", { name: `Tracks that start at once for ${kind} requests` }) as HTMLInputElement | null;
const quota = () => screen.getByRole("spinbutton", { name: /^Quota/ }) as HTMLInputElement;
const save = () => screen.getByRole("button", { name: "Save" });
/** The form is there once the policies are loaded (the heading is there before). */
const ready = () => screen.findByRole("button", { name: "Save" });
const reset = () => screen.getByRole("button", { name: "Back to the general policy" });

beforeEach(() => {
  connectivityMock.reset();
  mockView.mockReset().mockResolvedValue(untouchedUserPolicy);
  mockSet.mockReset();
  mockClear.mockReset();
  Object.values(mockToast).forEach((fn) => fn.mockReset());
});

describe("a user's download policy", () => {
  it("each kind says what the general policy is and what applies now; a field left empty follows the general policy", async () => {
    renderPanel();

    await ready();
    expect(screen.getByRole("heading", { name: "Download policy of alice" })).toBeInTheDocument();
    expect(mode("Playlist").value).toBe("");
    expect(within(mode("Playlist")).getByRole("option", { name: "General policy: Needs approval" })).toBeInTheDocument();
    expect(within(mode("Album")).getByRole("option", { name: "General policy: Automatic up to 30 tracks" })).toBeInTheDocument();
    expect(within(mode("Track")).getByRole("option", { name: "General policy: Automatic" })).toBeInTheDocument();
    expect(screen.getAllByText(/^Now: /).map((el) => el.textContent)).toEqual([
      "Now: Automatic",
      "Now: Automatic up to 30 tracks",
      "Now: Needs approval",
      "Now: Needs approval",
      "Now: Automatic up to 50 tracks",
    ]);
    expect(quota().value).toBe("");
    expect(quota().placeholder).toBe("200");
    expect(screen.getByText(/Empty follows the general quota \(200\); 0 means no quota for this user\./)).toBeInTheDocument();
    expect(screen.getByLabelText("Quota (tracks per 7 days)")).toBe(quota());
  });

  it("offers the three modes", async () => {
    renderPanel();
    await ready();

    expect(within(mode("Playlist")).getAllByRole("option").map((o) => o.textContent)).toEqual([
      "General policy: Needs approval",
      "Automatic",
      "Needs approval",
      "Automatic up to a limit",
    ]);
  });

  it("the threshold exists only where the rule that would apply is 'up to'", async () => {
    renderPanel();
    await ready();

    // The album follows the general policy, which is "up to 30": it can be given another threshold.
    expect(threshold("Album")?.placeholder).toBe("30");
    // The playlist follows "needs approval": nothing to set until it is given the "up to" mode.
    expect(threshold("Playlist")).toBeNull();
    fireEvent.change(mode("Playlist"), { target: { value: "auto_up_to" } });
    expect(threshold("Playlist")).not.toBeNull();
    fireEvent.change(mode("Playlist"), { target: { value: "auto" } });
    expect(threshold("Playlist")).toBeNull();
  });

  it("nothing to save until something changes", async () => {
    renderPanel();
    await ready();

    expect(save()).toBeDisabled();
    expect(reset()).toBeDisabled();
    fireEvent.change(mode("Playlist"), { target: { value: "auto" } });
    expect(save()).toBeEnabled();
  });

  it("saves only what was changed: a mode and threshold for one kind and the quota", async () => {
    mockSet.mockResolvedValue(customizedUserPolicy);
    renderPanel();
    await ready();

    fireEvent.change(mode("Playlist"), { target: { value: "auto_up_to" } });
    fireEvent.change(threshold("Playlist")!, { target: { value: "7" } });
    fireEvent.change(quota(), { target: { value: "9" } });
    fireEvent.click(save());

    await waitFor(() => expect(mockSet).toHaveBeenCalledWith("user-alice", { rules: { playlist: { mode: "auto_up_to", threshold: 7 } }, quota_tracks: 9 }));
    expect(mockToast.success).toHaveBeenCalledWith("Download policy saved");
    // What the server says now applies is shown.
    expect(await screen.findByText("Now: Automatic up to 7 tracks")).toBeInTheDocument();
    expect(screen.getByText(/Now: 9 tracks/)).toBeInTheDocument();
    expect(save()).toBeDisabled();
    expect(reset()).toBeEnabled();
  });

  it("a threshold alone, with the mode inherited, is a valid override", async () => {
    mockSet.mockResolvedValue(untouchedUserPolicy);
    renderPanel();
    await ready();

    fireEvent.change(threshold("Album")!, { target: { value: "12" } });
    fireEvent.click(save());

    await waitFor(() => expect(mockSet).toHaveBeenCalledWith("user-alice", { rules: { album: { mode: null, threshold: 12 } }, quota_tracks: null }));
  });

  it("a quota of 0 is a quota of its own: no quota for this user", async () => {
    mockSet.mockResolvedValue({ ...untouchedUserPolicy, override: { rules: {}, quota_tracks: 0 }, effective: { ...generalPolicy, quota: { tracks: 0, window_days: 7 } } });
    renderPanel();
    await ready();

    fireEvent.change(quota(), { target: { value: "0" } });
    fireEvent.click(save());

    await waitFor(() => expect(mockSet).toHaveBeenCalledWith("user-alice", { rules: {}, quota_tracks: 0 }));
    expect(await screen.findByText(/Now: no quota/)).toBeInTheDocument();
  });

  it("a threshold below 1 or a negative or fractional quota cannot be saved", async () => {
    renderPanel();
    await ready();

    fireEvent.change(threshold("Album")!, { target: { value: "0" } });
    expect(save()).toBeDisabled();
    fireEvent.change(threshold("Album")!, { target: { value: "" } });
    fireEvent.change(quota(), { target: { value: "-1" } });
    expect(save()).toBeDisabled();
    fireEvent.change(quota(), { target: { value: "2.5" } });
    expect(save()).toBeDisabled();
    fireEvent.change(quota(), { target: { value: "5" } });
    expect(save()).toBeEnabled();
  });

  it("starts from what an admin already set, and 'back to the general policy' clears it", async () => {
    mockView.mockResolvedValue(customizedUserPolicy);
    mockClear.mockResolvedValue(untouchedUserPolicy);
    renderPanel();
    await ready();

    expect(mode("Playlist").value).toBe("auto_up_to");
    expect(threshold("Playlist")!.value).toBe("7");
    expect(quota().value).toBe("9");
    expect(save()).toBeDisabled();
    expect(reset()).toBeEnabled();

    fireEvent.click(reset());

    await waitFor(() => expect(mockClear).toHaveBeenCalledWith("user-alice"));
    expect(mockToast.success).toHaveBeenCalledWith("This user follows the general policy again");
    await waitFor(() => expect(mode("Playlist").value).toBe(""));
    expect(quota().value).toBe("");
    expect(reset()).toBeDisabled();
    expect(screen.getAllByText("Now: Needs approval")).toHaveLength(2);
  });

  it("giving a kind its inherited mode back drops the threshold typed for it", async () => {
    mockSet.mockResolvedValue(untouchedUserPolicy);
    renderPanel();
    await ready();

    fireEvent.change(mode("Playlist"), { target: { value: "auto_up_to" } });
    fireEvent.change(threshold("Playlist")!, { target: { value: "7" } });
    fireEvent.change(mode("Playlist"), { target: { value: "" } });
    fireEvent.change(quota(), { target: { value: "5" } });
    fireEvent.click(save());

    await waitFor(() => expect(mockSet).toHaveBeenCalledWith("user-alice", { rules: {}, quota_tracks: 5 }));
  });

  it("a save the server refuses says so", async () => {
    mockSet.mockRejectedValue(new Error("422"));
    renderPanel();
    await ready();

    fireEvent.change(quota(), { target: { value: "5" } });
    fireEvent.click(save());

    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith("Could not save the download policy"));
  });

  it("a policy that cannot be loaded says so", async () => {
    mockView.mockRejectedValue(new Error("network"));
    renderPanel();

    expect(await screen.findByText("Could not load the download policy")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });

  it("every control is a 44px target on touch screens", async () => {
    renderPanel();
    await ready();

    for (const control of [mode("Playlist"), quota()]) expect(control.className).toContain("h-11");
  });

  it("in Italian", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    renderPanel();

    await screen.findByRole("combobox", { name: "Regola per le richieste di Playlist" });
    expect(screen.getByRole("heading", { name: "Policy di download di alice" })).toBeInTheDocument();
    expect(screen.getByText("Ora: Automatica fino a 30 brani")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Regola per le richieste di Playlist" })).toBeInTheDocument();
    // The playlist and the artist follow the general policy, which is "needs approval" for both.
    expect(screen.getAllByRole("option", { name: "Policy generale: Serve approvazione" })).toHaveLength(2);
  });
});

describe("what it is given", () => {
  it("is read from the general policy and the user's own, by the user's id", async () => {
    const view: UserDownloadPolicyView = untouchedUserPolicy;
    mockView.mockResolvedValue(view);
    renderPanel();
    await ready();

    expect(mockView).toHaveBeenCalledWith("user-alice");
  });
});
