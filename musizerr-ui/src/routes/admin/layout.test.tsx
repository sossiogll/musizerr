import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor, within } from "@testing-library/react";
import { LOCALE_STORAGE_KEY } from "@/i18n";
import { renderPage } from "@/test/render";
import { viewerStatus } from "@/test/viewer";
import { queryKeys } from "@/lib/query-keys";
import type { UpdatesInfo } from "@/lib/api-client";
import { AdminLayout } from "./layout";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("admin"), user: { id: "root" } }) }));

let pending = 3;
let updates: UpdatesInfo;
vi.mock("@/lib/api-client", () => ({ getPendingRequestCount: async () => ({ pending }), getUpdates: async () => updates }));

const NONE: UpdatesInfo = {
  current: "1.2.0", commit: "abc1234def", development: false, check_enabled: true, checked_at: "2026-10-17T08:00:00+00:00",
  last_check_failed: false, release_info_unavailable: null, updates: [], latest: "1.2.0",
};

const renderLayout = () =>
  renderPage(AdminLayout, { path: "/admin/users", otherPaths: ["/admin/requests", "/admin/routines", "/admin/sources", "/admin/duplicates", "/admin/system"] });
const requestsTab = async () => within(await screen.findByRole("navigation", { name: "Administration sections" })).getByRole("link", { name: /Requests/ });

beforeEach(() => {
  pending = 3;
  updates = NONE;
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

describe("Administration › a newer version of Musizerr", () => {
  const notice = () => screen.findByRole("status", { name: /is available/ });

  it("says nothing while the version that runs is the newest", async () => {
    renderLayout();
    await requestsTab();
    expect(screen.queryByRole("status", { name: /is available/ })).not.toBeInTheDocument();
  });

  it("an update that only needs its images: what it is and how to apply it by hand", async () => {
    updates = {
      ...NONE, latest: "1.3.0",
      updates: [{ version: "1.3.0", url: "https://github.com/r/v1.3.0", published_at: null, kind: "minor", plain: true, reasons: [] }],
    };
    renderLayout();

    const box = await notice();
    expect(box).toHaveTextContent("Musizerr 1.3.0 is available");
    expect(box).toHaveTextContent("Minor version · you run 1.2.0");
    expect(box).toHaveTextContent("Pulling its images is all it takes");
    expect(box).toHaveTextContent("set MUSIZERR_VERSION=1.3.0 in .env, then run docker compose pull && docker compose up -d");
    expect(within(box).getByRole("link", { name: "Release notes" })).toHaveAttribute("href", "https://github.com/r/v1.3.0");
  });

  it("an update that needs care says why, and counts the other releases", async () => {
    updates = {
      ...NONE, latest: "2.0.0",
      updates: [
        {
          version: "2.0.0", url: null, published_at: null, kind: "major", plain: false,
          reasons: [{ code: "major" }, { code: "contract_unsupported", providers: ["lyrics", "catalog"] }, { code: "upgrade_path", through: "1.9.0" }],
        },
        { version: "1.3.0", url: null, published_at: null, kind: "minor", plain: true, reasons: [] },
      ],
    };
    renderLayout();

    const box = await notice();
    expect(box).toHaveTextContent("Musizerr 2.0.0 is available");
    expect(box).toHaveTextContent("1 more release");
    const reasons = within(box).getAllByRole("listitem").map((item) => item.textContent);
    expect(reasons).toEqual([
      "A major version: read the release notes before updating.",
      "These providers would stop until they are updated: lyrics and catalog.",
      "Update to 1.9.0 first, then to this version.",
    ]);
    expect(box).not.toHaveTextContent("Pulling its images is all it takes");
  });

  it("in Italian", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "it");
    updates = {
      ...NONE, latest: "1.2.1",
      updates: [{ version: "1.2.1", url: null, published_at: null, kind: "patch", plain: true, reasons: [] }],
    };
    renderLayout();
    expect(await screen.findByRole("status", { name: "È disponibile Musizerr 1.2.1" })).toHaveTextContent("Patch · stai usando la 1.2.0");
  });
});
