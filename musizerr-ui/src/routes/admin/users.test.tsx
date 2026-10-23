import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { viewerStatus } from "@/test/viewer";
import { connectivityMock } from "@/test/connectivity-mock";
import { generalPolicy, untouchedUserPolicy } from "@/test/download-requests";
import { renderPage } from "@/test/render";
import { AdminUsersPage } from "./users";

let viewer: "admin" | "open" = "admin";
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ authConfig: viewerStatus(viewer), user: viewer === "admin" ? { id: "me" } : null }),
}));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());
vi.mock("@/components/ui/dropdown-menu", async () => (await import("@/test/dropdown-mock")).dropdownModule());
const mockToast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock("sonner", () => ({ toast: mockToast }));

const root = { id: "me", username: "root", email: "root@example.com", role: "admin", is_active: true };
const alice = { id: "u2", username: "alice", email: null, role: "user", is_active: false };
let accounts = [root, alice];
const mockUpdate = vi.fn();
const mockDelete = vi.fn();
const mockCreate = vi.fn();
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  adminListUsers: async () => ({ items: accounts, total: accounts.length }),
  adminCreateUser: (...a: unknown[]) => mockCreate(...a),
  adminUpdateUser: (...a: unknown[]) => mockUpdate(...a),
  adminDeleteUser: (id: string) => mockDelete(id),
  getUserDownloadPolicy: async () => untouchedUserPolicy,
  getDownloadPolicy: async () => generalPolicy,
}));

const renderUsers = () => renderPage(AdminUsersPage, { path: "/admin/users" });

beforeEach(() => {
  connectivityMock.reset();
  viewer = "admin";
  accounts = [root, alice];
  mockUpdate.mockReset().mockResolvedValue({});
  mockDelete.mockReset().mockResolvedValue({});
  mockCreate.mockReset();
  mockToast.success.mockReset();
  mockToast.error.mockReset();
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

function conflict(code: string) {
  const response = {
    status: 409,
    data: { detail: { error_code: code, error_message: "The username is already taken." } },
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
  } as AxiosResponse;
  return new AxiosError("conflict", "409", undefined, undefined, response);
}

async function openNewUser() {
  fireEvent.click(await screen.findByRole("button", { name: "New user" }));
  return screen.findByRole("alertdialog", { name: "New user" });
}

function fill(dialog: HTMLElement, fields: { username?: string; email?: string; password?: string; confirm?: string }) {
  const set = (label: string, value?: string) =>
    value !== undefined && fireEvent.change(within(dialog).getByLabelText(label), { target: { value } });
  set("Username", fields.username);
  set("Email (optional)", fields.email);
  set("Password", fields.password);
  set("Confirm password", fields.confirm);
}

describe("Admin users — New user", () => {
  it("creates an account with the fields as the server takes them, then lists it", async () => {
    mockCreate.mockImplementation(async (body: { username: string }) => {
      const created = { id: "u9", username: body.username, email: "dave@example.com", role: "user", is_active: true };
      accounts = [...accounts, created];
      return created;
    });
    renderUsers();

    const dialog = await openNewUser();
    expect(within(dialog).getByRole("button", { name: "Create account" })).toBeDisabled();
    fill(dialog, { username: " dave ", email: "dave@example.com", password: "correct-horse", confirm: "correct-horse" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create account" }));

    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith({
        username: "dave",
        password: "correct-horse",
        email: "dave@example.com",
        role: "user",
      }),
    );
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith("Account dave created"));
    expect(await screen.findByText("dave")).toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("can make an administrator, and leaves the email out when there is none", async () => {
    mockCreate.mockResolvedValue({ id: "u9", username: "boss", email: null, role: "admin", is_active: true });
    renderUsers();

    const dialog = await openNewUser();
    fill(dialog, { username: "boss", password: "correct-horse", confirm: "correct-horse" });
    fireEvent.change(within(dialog).getByLabelText("Role"), { target: { value: "admin" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create account" }));

    await waitFor(() =>
      expect(mockCreate).toHaveBeenCalledWith({ username: "boss", password: "correct-horse", role: "admin" }),
    );
  });

  it("does not let a request leave that cannot succeed: no name, a short password, passwords that differ", async () => {
    renderUsers();
    const dialog = await openNewUser();
    const create = within(dialog).getByRole("button", { name: "Create account" });

    fill(dialog, { password: "correct-horse", confirm: "correct-horse" });
    expect(create).toBeDisabled(); // no username

    fill(dialog, { username: "dave", password: "short", confirm: "short" });
    expect(create).toBeDisabled();
    expect(within(dialog).getByText("At least 8 characters.")).toBeInTheDocument();

    fill(dialog, { password: "correct-horse", confirm: "correct-horse-2" });
    expect(create).toBeDisabled();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Passwords do not match");

    fill(dialog, { confirm: "correct-horse" });
    expect(create).toBeEnabled();
    expect(mockCreate).not.toHaveBeenCalled();
  });

  it("says a taken username in words, from the server's code, and keeps the form", async () => {
    mockCreate.mockRejectedValue(conflict("username_taken"));
    renderUsers();

    const dialog = await openNewUser();
    fill(dialog, { username: "alice", password: "correct-horse", confirm: "correct-horse" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create account" }));

    expect(await within(dialog).findByText("That username is already taken.")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Username")).toHaveValue("alice");
  });

  it("says a taken email in words too", async () => {
    mockCreate.mockRejectedValue(conflict("email_taken"));
    renderUsers();

    const dialog = await openNewUser();
    fill(dialog, { username: "dave", password: "correct-horse", confirm: "correct-horse" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create account" }));

    expect(await within(dialog).findByText("That email is already used by another account.")).toBeInTheDocument();
  });

  it("cannot be sent while the server is unreachable", async () => {
    connectivityMock.set({ online: false });
    renderUsers();

    const dialog = await openNewUser();
    fill(dialog, { username: "dave", password: "correct-horse", confirm: "correct-horse" });

    expect(within(dialog).getByRole("button", { name: "Create account" })).toBeDisabled();
  });

  it("Cancel closes the dialog and creates nothing", async () => {
    renderUsers();
    const dialog = await openNewUser();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(mockCreate).not.toHaveBeenCalled();
  });
});

describe("Admin users — with authentication off", () => {
  it("lists the accounts that were created, says they wait for authentication, and offers New user", async () => {
    viewer = "open";
    accounts = [{ id: "u2", username: "alice", email: null, role: "user", is_active: true }];
    renderUsers();

    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText(/Authentication is off, so nobody signs in yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New user" })).toBeInTheDocument();
    // The owner is nobody on the list: there is no "(you)", and every account has its menu.
    expect(screen.queryByText("(you)")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Actions for alice" })).toBeEnabled();
  });

  it("says there are none yet instead of showing an empty box", async () => {
    viewer = "open";
    accounts = [];
    renderUsers();

    expect(await screen.findByText("No accounts yet.")).toBeInTheDocument();
  });

  it("does not say it with authentication on", async () => {
    renderUsers();
    await screen.findByText("alice");

    expect(screen.queryByText(/Authentication is off/)).not.toBeInTheDocument();
  });
});
