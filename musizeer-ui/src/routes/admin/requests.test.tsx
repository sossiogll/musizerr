import { beforeEach, describe, expect, it, vi } from "vitest";
import { AxiosError, type AxiosResponse } from "axios";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { LOCALE_STORAGE_KEY } from "@/i18n";
import { connectivityMock } from "@/test/connectivity-mock";
import { approvedAlbum, asAdmin, decision, waitingAlbum, waitingLikes, waitingPlaylist } from "@/test/download-requests";
import { renderPage } from "@/test/render";
import { viewerStatus } from "@/test/viewer";
import { AdminRequestsPage } from "./requests";
import type { AdminDownloadRequest } from "@/types";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("admin"), user: { id: "root" } }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
const mockToast = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), error: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const mockList = vi.fn();
const mockApprove = vi.fn();
const mockDeny = vi.fn();
vi.mock("@/lib/api-client", () => ({
  getAdminRequests: (...a: unknown[]) => mockList(...a),
  approveRequests: (...a: unknown[]) => mockApprove(...a),
  denyRequests: (...a: unknown[]) => mockDeny(...a),
  getPendingRequestCount: async () => ({ pending: 3 }),
  adminListUsers: async () => ({
    items: [
      { id: "user-alice", username: "alice", email: null, role: "user", is_active: true },
      { id: "user-bob", username: "bob", email: null, role: "user", is_active: true },
    ],
    total: 2,
  }),
}));

const playlist = asAdmin(waitingPlaylist, "alice");
const album = asAdmin(waitingAlbum, "bob");
const likes = asAdmin(waitingLikes, "alice");
const approved = asAdmin(approvedAlbum, "alice", "root");

const page = (items: AdminDownloadRequest[], over: { pages?: number; total?: number } = {}) => ({
  items,
  total: over.total ?? items.length,
  page: 1,
  limit: 25,
  pages: over.pages ?? 1,
});

const renderRequests = () => renderPage(AdminRequestsPage, { path: "/admin/requests", otherPaths: ["/config/$section"] });

beforeEach(() => {
  connectivityMock.reset();
  mockList.mockReset().mockResolvedValue(page([playlist, album, likes]));
  mockApprove.mockReset().mockResolvedValue([]);
  mockDeny.mockReset().mockResolvedValue([]);
  Object.values(mockToast).forEach((fn) => fn.mockReset());
});

describe("Administration › Requests: the list", () => {
  it("asks for the waiting ones first, a page at a time, and shows who asked, what, how many tracks, why and when", async () => {
    renderRequests();

    const rows = await screen.findAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(mockList).toHaveBeenCalledWith({ page: 1, limit: 25, status: ["pending"], kind: undefined, userId: undefined });

    const first = within(rows[0]);
    expect(first.getByRole("link", { name: "Rap 2010" })).toHaveAttribute("href", "/playlist/catalog/playlist-259");
    expect(first.getByText(/^Asked by alice/)).toBeInTheDocument();
    expect(first.getByText("Playlist")).toBeInTheDocument();
    expect(first.getByText("259 tracks to download")).toBeInTheDocument();
    expect(first.getByText("This kind always needs approval")).toBeInTheDocument();
    expect(first.getByText("Waiting")).toBeInTheDocument();
    expect(first.getByText(/^Asked by alice/).querySelector("time")).toHaveAttribute("dateTime", "2026-10-04T08:00:00+00:00");

    expect(within(rows[1]).getByText("Larger than the automatic limit")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Someone")).toBeInTheDocument();
    expect(within(rows[2]).getByRole("link", { name: "Liked elsewhere" })).toHaveAttribute("href", "/favorites?view=elsewhere");
  });

  it("the Waiting tab carries the count the server gave, and the page leads to the policy", async () => {
    renderRequests();

    expect(await screen.findByRole("button", { name: /Waiting\s*3/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("link", { name: "Edit the approval policy" })).toHaveAttribute("href", "/config/library");
  });

  it("a decided request shows who decided it and the note, and has no actions or checkbox", async () => {
    mockList.mockResolvedValue(page([approved]));
    renderRequests();

    const decided = await screen.findByRole("listitem");
    expect(within(decided).getByText("Approved")).toBeInTheDocument();
    expect(within(decided).getByText(/^Decided by root/)).toBeInTheDocument();
    expect(within(decided).getByText("Note: Enjoy")).toBeInTheDocument();
    expect(within(decided).queryByRole("button")).not.toBeInTheDocument();
    expect(within(decided).queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("filters by state, kind and requester, each from the first page", async () => {
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("button", { name: "Denied" }));
    await waitFor(() => expect(mockList).toHaveBeenLastCalledWith({ page: 1, limit: 25, status: ["denied"], kind: undefined, userId: undefined }));

    fireEvent.click(screen.getByRole("button", { name: "All" }));
    await waitFor(() => expect(mockList).toHaveBeenLastCalledWith({ page: 1, limit: 25, status: undefined, kind: undefined, userId: undefined }));

    fireEvent.click(screen.getByRole("button", { name: "Kind" }));
    fireEvent.click(screen.getByRole("menuitemradio", { name: "Playlist" }));
    await waitFor(() => expect(mockList).toHaveBeenLastCalledWith({ page: 1, limit: 25, status: undefined, kind: ["playlist"], userId: undefined }));

    fireEvent.click(screen.getByRole("button", { name: "Requested by" }));
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "bob" }));
    await waitFor(() => expect(mockList).toHaveBeenLastCalledWith({ page: 1, limit: 25, status: undefined, kind: ["playlist"], userId: "user-bob" }));
  });

  it("is paginated by the server", async () => {
    mockList.mockResolvedValue(page([playlist], { pages: 4, total: 100 }));
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(mockList).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 })));
    expect(screen.getByText("2 / 4")).toBeInTheDocument();
  });

  it("says when nothing waits, and when a filter matches nothing", async () => {
    mockList.mockResolvedValue(page([]));
    renderRequests();
    expect(await screen.findByText("Nothing is waiting for approval.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Denied" }));
    expect(await screen.findByText("No requests match.")).toBeInTheDocument();
  });

  it("a list that cannot be loaded says so", async () => {
    mockList.mockRejectedValue(new Error("network"));
    renderRequests();
    expect(await screen.findByText("Could not load the requests")).toBeInTheDocument();
  });
});

describe("deciding one request", () => {
  it("approve: asks for an optional note, then approves exactly that request", async () => {
    mockApprove.mockResolvedValue([decision("req-playlist", "decided", { ...playlist, status: "approved" })]);
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("button", { name: "Approve Rap 2010" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Approve this request?" });
    expect(dialog).toHaveTextContent("The tracks are queued now and join the requester's favorites when they arrive.");
    fireEvent.change(within(dialog).getByLabelText("Note for the requester (optional)"), { target: { value: "  Have fun " } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith(["req-playlist"], "Have fun"));
    expect(mockDeny).not.toHaveBeenCalled();
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith("1 approved"));
  });

  it("deny: asks for the reason the requester will read, then denies", async () => {
    mockDeny.mockResolvedValue([decision("req-album", "decided", { ...album, status: "denied" })]);
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("button", { name: "Deny Box Set" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Deny this request?" });
    fireEvent.change(within(dialog).getByLabelText("Reason, shown to the requester (optional)"), { target: { value: "Not this week" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Deny" }));

    await waitFor(() => expect(mockDeny).toHaveBeenCalledWith(["req-album"], "Not this week"));
    expect(mockApprove).not.toHaveBeenCalled();
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith("1 denied"));
  });

  it("the note is optional, and cancelling the dialog decides nothing", async () => {
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("button", { name: "Approve Rap 2010" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(mockApprove).not.toHaveBeenCalled();

    mockApprove.mockResolvedValue([decision("req-playlist", "decided", { ...playlist, status: "approved" })]);
    fireEvent.click(screen.getByRole("button", { name: "Approve Rap 2010" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith(["req-playlist"], ""));
  });

  it("a decision the server could not take is said, in words", async () => {
    mockApprove.mockRejectedValue(new AxiosError("503", "ERR_BAD_RESPONSE", undefined, undefined, { status: 503, data: { detail: { error_code: "database_busy", error_message: "busy" } } } as AxiosResponse));
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("button", { name: "Approve Rap 2010" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith("The server is busy, try again in a moment."));
  });
});

describe("deciding many requests at once", () => {
  it("select some, approve them in one call (the batch route) with one note", async () => {
    mockApprove.mockResolvedValue([decision("req-playlist", "decided", playlist), decision("req-likes", "decided", likes)]);
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Rap 2010" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select Liked elsewhere" }));
    expect(screen.getByText("2 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve 2" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Approve 2 requests?" });
    fireEvent.change(within(dialog).getByLabelText("Note for the requester (optional)"), { target: { value: "all good" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(mockApprove).toHaveBeenCalledWith(["req-playlist", "req-likes"], "all good"));
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith("2 approved"));
    // The selection is spent.
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
  });

  it("select all the waiting ones of the page, then deny them with a reason", async () => {
    mockDeny.mockResolvedValue([decision("req-playlist", "decided", playlist), decision("req-album", "decided", album), decision("req-likes", "decided", likes)]);
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("checkbox", { name: "Select the 3 waiting requests" }));
    expect(screen.getByText("3 selected")).toBeInTheDocument();
    expect(screen.getAllByRole("checkbox", { name: /^Select (Rap|Box|Liked)/ }).every((box) => (box as HTMLInputElement).checked)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Deny 3" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Deny 3 requests?" });
    fireEvent.change(within(dialog).getByLabelText("Reason, shown to the requester (optional)"), { target: { value: "Over budget" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Deny" }));

    await waitFor(() => expect(mockDeny).toHaveBeenCalledWith(["req-playlist", "req-album", "req-likes"], "Over budget"));
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith("3 denied"));
  });

  it("the selection can be cleared, and a new filter starts without one", async () => {
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Rap 2010" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "Select Rap 2010" }));
    fireEvent.click(screen.getByRole("button", { name: "All" }));
    await waitFor(() => expect(screen.queryByText("1 selected")).not.toBeInTheDocument());
  });
});

describe("what each decision came to", () => {
  const extra = (id: string, title: string): AdminDownloadRequest => ({ ...playlist, id, title });
  const five = [extra("a", "Alpha"), extra("b", "Bravo"), extra("c", "Charlie"), extra("d", "Delta"), extra("e", "Echo")];

  async function approveAll() {
    mockList.mockResolvedValue(page(five));
    renderRequests();
    await screen.findAllByRole("listitem");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select the 5 waiting requests" }));
    fireEvent.click(screen.getByRole("button", { name: "Approve 5" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Approve" }));
  }

  it("every outcome in words: decided, unchanged, decided otherwise, gone, could not be queued", async () => {
    mockApprove.mockResolvedValue([
      decision("a", "decided", { ...five[0], status: "approved" }),
      decision("b", "unchanged", { ...five[1], status: "approved" }),
      decision("c", "conflict", { ...five[2], status: "denied" }),
      decision("d", "not_found", null),
      decision("e", "failed", null),
    ]);
    await approveAll();

    const report = await screen.findByRole("status");
    expect(report).toHaveTextContent(
      "1 approved · 1 was already approved · 1 was decided otherwise meanwhile · 1 no longer exists · 1 could not be queued and stays waiting",
    );
    // Only the requests that did not simply go through are listed, by name, with what happened to each.
    const lines = within(report).getAllByRole("listitem");
    expect(lines.map((line) => line.textContent)).toEqual([
      "BravoAlready approved: nothing changed",
      "CharlieDecided otherwise: Denied",
      "DeltaNo longer exists",
      "EchoCould not be queued: still waiting, try again",
    ]);
    expect(mockToast.warning).toHaveBeenCalledWith("1 of 5 decided: see the details");
    expect(mockToast.success).not.toHaveBeenCalled();
  });

  it("when everything went through the report says only that", async () => {
    mockApprove.mockResolvedValue(five.map((r) => decision(r.id, "decided", { ...r, status: "approved" })));
    await approveAll();

    const report = await screen.findByRole("status");
    expect(report).toHaveTextContent("5 approved");
    expect(within(report).queryByRole("listitem")).not.toBeInTheDocument();
    expect(mockToast.success).toHaveBeenCalledWith("5 approved");
  });

  it("denying says 'denied' and 'already denied'", async () => {
    mockDeny.mockResolvedValue([decision("req-playlist", "decided", playlist), decision("req-album", "unchanged", { ...album, status: "denied" })]);
    mockList.mockResolvedValue(page([playlist, album]));
    renderRequests();
    await screen.findAllByRole("listitem");
    fireEvent.click(screen.getByRole("checkbox", { name: "Select the 2 waiting requests" }));
    fireEvent.click(screen.getByRole("button", { name: "Deny 2" }));
    fireEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Deny" }));

    const report = await screen.findByRole("status");
    expect(report).toHaveTextContent("1 denied · 1 was already denied");
    expect(within(report).getByText("Already denied: nothing changed")).toBeInTheDocument();
  });

  it("the report can be dismissed", async () => {
    mockApprove.mockResolvedValue(five.map((r) => decision(r.id, "decided", r)));
    await approveAll();

    fireEvent.click(await screen.findByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("the list is read again after a decision", async () => {
    mockApprove.mockResolvedValue(five.map((r) => decision(r.id, "decided", r)));
    await approveAll();

    await waitFor(() => expect(mockList.mock.calls.length).toBeGreaterThan(1));
  });
});

describe("in Italian", () => {
  it("speaks Italian, with its plurals", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    renderRequests();
    await screen.findAllByRole("listitem");

    fireEvent.click(screen.getByRole("checkbox", { name: "Seleziona le 3 richieste in attesa" }));
    expect(screen.getByText("3 selezionate")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approva 3" }));
    expect(await screen.findByRole("alertdialog", { name: "Approvare 3 richieste?" })).toBeInTheDocument();
  });
});
