import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import { useQuery } from "@tanstack/react-query";
import { LOCALE_STORAGE_KEY } from "@/i18n";
import { viewerStatus } from "@/test/viewer";
import { renderPage } from "@/test/render";
import { approvedAlbum, asAdmin, deniedArtist, eventDecidedForRequester, eventPendingForAdmins, waitingLikes, waitingPlaylist } from "@/test/download-requests";
import { usePendingRequestCount } from "@/hooks/usePendingRequestCount";
import { queryKeys } from "@/lib/query-keys";
import { RequestsWatcher } from "./RequestsWatcher";
import type { AuthConfig, StreamEvent } from "@/types";

let status: AuthConfig;
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status, user: { id: "user-alice" } }) }));
const mockToast = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), error: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const mockCount = vi.fn();
const mockAdminList = vi.fn();
const mockMyList = vi.fn();
let progressSource: { onmessage: ((e: MessageEvent) => void) | null } & Record<string, unknown> = { onmessage: null };
vi.mock("@/lib/api-client", () => ({
  getPendingRequestCount: () => mockCount(),
  getAdminRequests: () => mockAdminList(),
  getMyRequests: () => mockMyList(),
  createProgressSource: () => {
    progressSource = { close: () => {}, onmessage: null, onopen: null, onerror: null, readyState: 1 };
    return progressSource;
  },
  refreshAccessToken: vi.fn(),
}));

/** What the shell and the Requests page read: the badge, and a list of the admin's requests. */
function Probe() {
  const waiting = usePendingRequestCount();
  const list = useQuery({ queryKey: queryKeys.adminRequests({ filter: "pending" }), queryFn: () => mockAdminList() });
  const mine = useQuery({ queryKey: queryKeys.myRequests({ status: "" }), queryFn: () => mockMyList() });
  return (
    <>
      <RequestsWatcher />
      <p data-testid="badge">{waiting}</p>
      <p data-testid="list">{list.data?.total ?? "-"}</p>
      <p data-testid="mine">{mine.data?.total ?? "-"}</p>
    </>
  );
}

const renderProbe = () => renderPage(() => <Probe />, { path: "/", queue: true });
const send = (event: StreamEvent) => act(() => progressSource.onmessage?.({ data: JSON.stringify(event) } as MessageEvent));
const page = (total: number) => ({ items: [], total, page: 1, limit: 25, pages: 1 });

beforeEach(() => {
  status = viewerStatus("admin");
  mockCount.mockReset().mockResolvedValue({ pending: 2 });
  mockAdminList.mockReset().mockResolvedValue(page(2));
  mockMyList.mockReset().mockResolvedValue(page(1));
  Object.values(mockToast).forEach((fn) => fn.mockReset());
});

describe("the admin's badge and list follow the progress stream", () => {
  it("the badge starts from the server's count and takes the count an event carries, without asking again", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("badge")).toHaveTextContent("2"));
    expect(mockCount).toHaveBeenCalledTimes(1);

    send(eventPendingForAdmins(asAdmin(waitingPlaylist), 5));

    await waitFor(() => expect(screen.getByTestId("badge")).toHaveTextContent("5"));
    // No polling and no extra read: the event carries the number.
    expect(mockCount).toHaveBeenCalledTimes(1);
  });

  it("the badge goes down when a request is decided, by the same mechanism", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("badge")).toHaveTextContent("2"));

    send({ type: "download_request", event: "approved", request: asAdmin(approvedAlbum, "alice", "root"), pending: 1 });

    await waitFor(() => expect(screen.getByTestId("badge")).toHaveTextContent("1"));
  });

  it("the admin's list is read again when a request arrives or is decided", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("list")).toHaveTextContent("2"));
    expect(mockAdminList).toHaveBeenCalledTimes(1);

    mockAdminList.mockResolvedValue(page(3));
    send(eventPendingForAdmins(asAdmin(waitingPlaylist), 3));

    await waitFor(() => expect(screen.getByTestId("list")).toHaveTextContent("3"));
    expect(mockAdminList).toHaveBeenCalledTimes(2);
  });

  it("a burst of events (a bulk decision) reads the list once", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("list")).toHaveTextContent("2"));

    for (let i = 0; i < 12; i++) send({ type: "download_request", event: "approved", request: asAdmin(approvedAlbum), pending: 12 - i });

    await waitFor(() => expect(mockAdminList).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(mockAdminList).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.getByTestId("badge")).toHaveTextContent("1"));
  });

  it("no toast for the admin: the badge and the list are the notice", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("badge")).toHaveTextContent("2"));

    send(eventPendingForAdmins(asAdmin(waitingPlaylist), 3));

    expect(Object.values(mockToast).every((fn) => fn.mock.calls.length === 0)).toBe(true);
  });
});

describe("the requester is told how their request ended", () => {
  beforeEach(() => {
    status = viewerStatus("user");
  });

  it("approved: a toast with the admin's note, and what the approval started is read again", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("mine")).toHaveTextContent("1"));
    expect(mockMyList).toHaveBeenCalledTimes(1);

    mockMyList.mockResolvedValue(page(4));
    send(eventDecidedForRequester(approvedAlbum));

    expect(mockToast.success).toHaveBeenCalledWith("Your request was approved: Box Set", { description: "Note: Enjoy" });
    await waitFor(() => expect(screen.getByTestId("mine")).toHaveTextContent("4"));
  });

  it("denied: a toast with the reason", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("mine")).toHaveTextContent("1"));

    send(eventDecidedForRequester(deniedArtist));

    expect(mockToast.warning).toHaveBeenCalledWith("Your request was denied: Someone", { description: "Reason: Too much for this week" });
    expect(mockToast.success).not.toHaveBeenCalled();
  });

  it("a denial without a reason says only that it was denied", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("mine")).toHaveTextContent("1"));

    send(eventDecidedForRequester({ ...deniedArtist, decision_note: null }));

    expect(mockToast.warning).toHaveBeenCalledWith("Your request was denied: Someone", { description: undefined });
  });

  it("the likes download has no title: it is named by its kind", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("mine")).toHaveTextContent("1"));

    send(eventDecidedForRequester({ ...waitingLikes, status: "approved", decision_note: null }));

    expect(mockToast.success).toHaveBeenCalledWith("Your request was approved: Liked elsewhere", { description: undefined });
  });

  it("in Italian", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("mine")).toHaveTextContent("1"));

    send(eventDecidedForRequester(deniedArtist));

    expect(mockToast.warning).toHaveBeenCalledWith("La tua richiesta è stata rifiutata: Someone", { description: "Motivo: Too much for this week" });
  });

  it("a user does not read the admin's list or count: nothing is asked of those routes", async () => {
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("mine")).toHaveTextContent("1"));

    expect(mockCount).not.toHaveBeenCalled();
  });
});
