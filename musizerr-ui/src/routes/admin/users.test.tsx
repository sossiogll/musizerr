import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { viewerStatus } from "@/test/viewer";
import { connectivityMock } from "@/test/connectivity-mock";
import { generalPolicy, untouchedUserPolicy } from "@/test/download-requests";
import { renderPage } from "@/test/render";
import { AdminUsersPage } from "./users";

vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: viewerStatus("admin"), user: { id: "me" } }) }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());

const root = { id: "me", username: "root", email: "root@example.com", role: "admin", is_active: true };
const alice = { id: "u2", username: "alice", email: null, role: "user", is_active: false };
let accounts = [root, alice];
const mockUpdate = vi.fn();
const mockDelete = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  adminListUsers: async () => ({ items: accounts, total: accounts.length }),
  adminUpdateUser: (...a: unknown[]) => mockUpdate(...a),
  adminDeleteUser: (id: string) => mockDelete(id),
  getUserDownloadPolicy: async () => untouchedUserPolicy,
  getDownloadPolicy: async () => generalPolicy,
}));

const renderUsers = () => renderPage(AdminUsersPage, { path: "/admin/users" });

beforeEach(() => {
  connectivityMock.reset();
  accounts = [root, alice];
  mockUpdate.mockReset().mockResolvedValue({});
  mockDelete.mockReset().mockResolvedValue({});
});

describe("Admin users", () => {
  it("one row per account with role and state in words; no table to scroll", async () => {
    renderUsers();
    const rows = await screen.findAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[1]).getByText("alice")).toBeInTheDocument();
    expect(within(rows[1]).getByText("User")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Inactive")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("the own account's menu is greyed out", async () => {
    renderUsers();
    expect(await screen.findByRole("button", { name: "Actions for root" })).toBeDisabled();
  });

  it("promote and activate from the row menu", async () => {
    renderUsers();
    fireEvent.click(await screen.findByRole("button", { name: "Actions for alice" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Promote" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith("u2", { role: "admin" }));

    fireEvent.click(screen.getByRole("button", { name: "Actions for alice" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Activate" }));
    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith("u2", { is_active: true }));
  });

  it("a user's menu has their download policy, which opens under their row; an admin has none", async () => {
    renderUsers();
    fireEvent.click(await screen.findByRole("button", { name: "Actions for alice" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Download policy" }));

    const aliceRow = screen.getByText("alice").closest("li") as HTMLElement;
    const panel = await within(aliceRow).findByRole("region", { name: "Download policy of alice" });
    expect(await within(panel).findByRole("combobox", { name: "Rule for Playlist requests" })).toBeInTheDocument();
    expect(screen.getAllByRole("region", { name: /^Download policy of/ })).toHaveLength(1);

    // The same menu item hides it again.
    fireEvent.click(screen.getByRole("button", { name: "Actions for alice" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Hide download policy" }));
    expect(screen.queryByRole("region", { name: /^Download policy of/ })).not.toBeInTheDocument();
  });

  it("an administrator is not held to the policy: no such item in their menu", async () => {
    accounts = [root, { id: "u3", username: "carol", email: null, role: "admin", is_active: true }];
    renderUsers();

    fireEvent.click(await screen.findByRole("button", { name: "Actions for carol" }));
    expect(screen.getByRole("menuitem", { name: "Demote" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /download policy/i })).not.toBeInTheDocument();
  });

  it("deleting asks first", async () => {
    renderUsers();
    fireEvent.click(await screen.findByRole("button", { name: "Actions for alice" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Delete alice?" });
    expect(mockDelete).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mockDelete).toHaveBeenCalledWith("u2"));
  });
});

describe("Admin users — accounts waiting for approval", () => {
  const carol = { id: "u3", username: "carol", email: null, role: "user", is_active: false, pending_approval: true };

  it("says an account created by a sign-in through a provider waits for approval, not that it is inactive", async () => {
    accounts = [root, carol];
    renderUsers();

    const row = (await screen.findAllByRole("listitem"))[1];
    expect(within(row).getByText("Waiting for approval")).toBeInTheDocument();
    expect(within(row).queryByText("Inactive")).not.toBeInTheDocument();
  });

  it("approving activates the account", async () => {
    accounts = [root, carol];
    renderUsers();

    fireEvent.click(await screen.findByRole("button", { name: "Actions for carol" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Approve" }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalledWith("u3", { is_active: true }));
  });
});
