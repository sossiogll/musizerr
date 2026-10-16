import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor, within } from "@testing-library/react";
import { LOCALE_STORAGE_KEY } from "@/i18n";
import { renderPage } from "@/test/render";
import { viewerStatus } from "@/test/viewer";
import { queryKeys } from "@/lib/query-keys";
import { AdminLayout } from "./layout";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("admin"), user: { id: "root" } }) }));

let pending = 3;
vi.mock("@/lib/api-client", () => ({ getPendingRequestCount: async () => ({ pending }) }));

const renderLayout = () =>
  renderPage(AdminLayout, { path: "/admin/users", otherPaths: ["/admin/requests", "/admin/routines", "/admin/sources", "/admin/duplicates", "/admin/system"] });
const requestsTab = async () => within(await screen.findByRole("navigation", { name: "Administration sections" })).getByRole("link", { name: /Requests/ });

beforeEach(() => {
  pending = 3;
});

describe("Administration › Requests tab", () => {
  it("carries the number of requests waiting for approval", async () => {
    renderLayout();

    const tab = await requestsTab();
    expect(await within(tab).findByLabelText("3 requests waiting for approval")).toHaveTextContent("3");
    expect(tab).toHaveAttribute("href", "/admin/requests");
    // Only that tab has one.
    expect(screen.getAllByLabelText(/waiting for approval/)).toHaveLength(1);
  });

  it("says '1 request' in the singular and shows nothing when none waits", async () => {
    pending = 1;
    const { queryClient } = renderLayout();
    expect(await within(await requestsTab()).findByLabelText("1 request waiting for approval")).toBeInTheDocument();

    act(() => queryClient.setQueryData(queryKeys.pendingRequestCount, { pending: 0 }));
    await waitFor(() => expect(screen.queryByLabelText(/waiting for approval/)).not.toBeInTheDocument());
  });

  it("follows the count the progress stream writes, without a request of its own", async () => {
    const { queryClient } = renderLayout();
    await within(await requestsTab()).findByLabelText("3 requests waiting for approval");

    act(() => queryClient.setQueryData(queryKeys.pendingRequestCount, { pending: 12 }));

    expect(await within(await requestsTab()).findByLabelText("12 requests waiting for approval")).toHaveTextContent("12");
  });

  it("in Italian", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    renderLayout();

    const nav = await screen.findByRole("navigation", { name: "Sezioni dell'amministrazione" });
    expect(await within(nav).findByLabelText("3 richieste in attesa di approvazione")).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /Richieste/ })).toBeInTheDocument();
  });
});
