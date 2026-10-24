import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { I18nProvider } from "@/i18n";
import { SettingsArea } from "./SettingsArea";
import type { ConfigSchemaResponse, SettingDef } from "@/lib/api-client";

// Turning authentication on from Administration › System. The API is mocked in the exact shapes the server
// answers (routes/config.py PATCH /settings, routes/admin.py POST /admin/users, docs in app/openapi.yaml).
const mockGetSettingsSchema = vi.fn();
const mockGetSettingsValues = vi.fn();
const mockPatchSettings = vi.fn();
const mockCreateUser = vi.fn();
const mockLogin = vi.fn();
const mockToast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));

vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  getSettingsSchema: (...a: unknown[]) => mockGetSettingsSchema(...a),
  getSettingsValues: (...a: unknown[]) => mockGetSettingsValues(...a),
  getProviders: async () => ({ metadata: [], download: [], lyrics: [], storage: [], sync: [], identification: [] }),
  patchSettings: (...a: unknown[]) => mockPatchSettings(...a),
  adminCreateUser: (...a: unknown[]) => mockCreateUser(...a),
}));
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ login: mockLogin }) }));
vi.mock("sonner", () => ({ toast: mockToast }));
vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const authentication: SettingDef = {
  key: "auth_enabled",
  type: "bool",
  group: "auth",
  area: "system",
  label: "Authentication",
  description: "",
  admin_only: true,
  options: null,
  minimum: null,
  maximum: null,
  step: null,
  secret: false,
  provider_category: null,
  required: true,
  visible_when: null,
};
const schema: ConfigSchemaResponse = {
  settings: [authentication],
  taxonomy: {
    areas: [{ key: "system", label_key: "settings.areas.system", label: "System" }],
    groups: [{ key: "auth", label_key: "settings.groups.auth", label: "Access", description_key: "settings.group_descriptions.auth", description: "", area: "system" }],
  },
};

function refusal(code: string, message: string, status = 400) {
  const response = {
    status,
    data: { detail: { error_code: code, error_message: message } },
    statusText: "",
    headers: {},
    config: { headers: new AxiosHeaders() },
  } as AxiosResponse;
  return new AxiosError("refused", String(status), undefined, undefined, response);
}

const NO_ADMIN = refusal("no_admin_account", "auth_enabled: Authentication cannot be turned on: no active administrator account exists to sign in with. Create an administrator first.");
const ADMIN = { id: "a1", username: "boss", email: null, role: "admin", is_active: true, pending_approval: false };

function renderSystem() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <SettingsArea area="system" />
      </I18nProvider>
    </QueryClientProvider>,
  );
  return queryClient;
}

async function askToTurnItOn() {
  fireEvent.click(await screen.findByRole("switch", { name: "Authentication" }));
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

async function guidedStep() {
  return screen.findByRole("alertdialog", { name: "Create the administrator account" });
}

function fill(dialog: HTMLElement, username: string, password: string, confirm = password) {
  fireEvent.change(within(dialog).getByLabelText("Username"), { target: { value: username } });
  fireEvent.change(within(dialog).getByLabelText("Password"), { target: { value: password } });
  fireEvent.change(within(dialog).getByLabelText("Confirm password"), { target: { value: confirm } });
}

beforeEach(() => {
  mockGetSettingsSchema.mockReset().mockResolvedValue(schema);
  mockGetSettingsValues.mockReset().mockResolvedValue({ auth_enabled: false });
  mockPatchSettings.mockReset();
  mockCreateUser.mockReset().mockResolvedValue(ADMIN);
  mockLogin.mockReset().mockResolvedValue(undefined);
  mockToast.success.mockReset();
  mockToast.error.mockReset();
});

describe("turning authentication on when nobody can sign in", () => {
  it("opens the guided step instead of an error when the server says no administrator exists", async () => {
    mockPatchSettings.mockRejectedValueOnce(NO_ADMIN);
    renderSystem();

    await askToTurnItOn();

    const dialog = await guidedStep();
    expect(within(dialog).getByText(/becomes this account's/)).toBeInTheDocument();
    expect(mockToast.error).not.toHaveBeenCalled();
    expect(mockCreateUser).not.toHaveBeenCalled();
  });

  it("creates the administrator, turns authentication on for them, and signs them in, in that order", async () => {
    const calls: string[] = [];
    mockPatchSettings.mockRejectedValueOnce(NO_ADMIN).mockImplementationOnce(async () => {
      calls.push("settings");
      return { auth_enabled: true };
    });
    mockCreateUser.mockImplementation(async () => {
      calls.push("create");
      return ADMIN;
    });
    mockLogin.mockImplementation(async () => {
      calls.push("login");
    });
    renderSystem();
    await askToTurnItOn();

    const dialog = await guidedStep();
    const submit = within(dialog).getByRole("button", { name: "Create and turn on" });
    expect(submit).toBeDisabled();
    fill(dialog, "boss", "correct-horse");
    fireEvent.click(submit);

    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith("boss", "correct-horse"));
    expect(calls).toEqual(["create", "settings", "login"]);
    expect(mockCreateUser).toHaveBeenCalledWith({ username: "boss", password: "correct-horse", role: "admin" });
    // The same batch the server refused, now naming the new administrator as the one who takes the owner's data.
    expect(mockPatchSettings).toHaveBeenLastCalledWith({ auth_enabled: true }, { adoptInto: "a1" });
    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith("Authentication is on. You are signed in as boss."));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("keeps the rest of the refused batch with the switch", async () => {
    mockGetSettingsSchema.mockResolvedValue({
      ...schema,
      settings: [authentication, { ...authentication, key: "other", type: "int", label: "Other", minimum: 0, maximum: 10, step: 1 }],
    });
    mockGetSettingsValues.mockResolvedValue({ auth_enabled: false, other: 1 });
    mockPatchSettings.mockRejectedValueOnce(NO_ADMIN).mockResolvedValueOnce({});
    renderSystem();

    fireEvent.change(await screen.findByDisplayValue("1"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("switch", { name: "Authentication" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const dialog = await guidedStep();
    fill(dialog, "boss", "correct-horse");
    fireEvent.click(within(dialog).getByRole("button", { name: "Create and turn on" }));

    await waitFor(() => expect(mockLogin).toHaveBeenCalled());
    expect(mockPatchSettings).toHaveBeenLastCalledWith({ other: 5, auth_enabled: true }, { adoptInto: "a1" });
  });

  it("does not let a password through that the server would refuse", async () => {
    mockPatchSettings.mockRejectedValueOnce(NO_ADMIN);
    renderSystem();
    await askToTurnItOn();
    const dialog = await guidedStep();
    const submit = within(dialog).getByRole("button", { name: "Create and turn on" });

    fill(dialog, "boss", "short");
    expect(submit).toBeDisabled();
    fill(dialog, "boss", "correct-horse", "correct-horse-2");
    expect(submit).toBeDisabled();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Passwords do not match");
    fill(dialog, "boss", "correct-horse");
    expect(submit).toBeEnabled();
  });

  it("a taken username is said in words and nothing else is attempted", async () => {
    mockPatchSettings.mockRejectedValueOnce(NO_ADMIN);
    mockCreateUser.mockRejectedValueOnce(refusal("username_taken", "The username is already taken.", 409));
    renderSystem();
    await askToTurnItOn();
    const dialog = await guidedStep();
    fill(dialog, "boss", "correct-horse");

    fireEvent.click(within(dialog).getByRole("button", { name: "Create and turn on" }));

    expect(await within(dialog).findByText("That username is already taken.")).toBeInTheDocument();
    expect(mockPatchSettings).toHaveBeenCalledTimes(1); // only the refused first attempt
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it("says the server's password refusal in words and goes no further", async () => {
    mockPatchSettings.mockRejectedValueOnce(NO_ADMIN);
    mockCreateUser.mockRejectedValueOnce(refusal("password_too_short", "The password must have at least 8 characters.", 422));
    renderSystem();
    await askToTurnItOn();
    const dialog = await guidedStep();
    fill(dialog, "boss", "correct-horse");

    fireEvent.click(within(dialog).getByRole("button", { name: "Create and turn on" }));

    expect(await within(dialog).findByText("That password is too short: it needs at least 8 characters.")).toBeInTheDocument();
    expect(mockPatchSettings).toHaveBeenCalledTimes(1);
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it("after a failure the next attempt goes on from where it stopped: the account is not created twice", async () => {
    mockPatchSettings
      .mockRejectedValueOnce(NO_ADMIN)
      .mockRejectedValueOnce(refusal("auth_secret_insecure", "auth_enabled: MUSIZEER_AUTH_SECRET is too short"))
      .mockResolvedValueOnce({ auth_enabled: true });
    renderSystem();
    await askToTurnItOn();
    const dialog = await guidedStep();
    fill(dialog, "boss", "correct-horse");

    fireEvent.click(within(dialog).getByRole("button", { name: "Create and turn on" }));
    expect(await within(dialog).findByText(/Authentication can't be turned on/)).toBeInTheDocument();
    expect(mockLogin).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Create and turn on" }));
    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith("boss", "correct-horse"));
    expect(mockCreateUser).toHaveBeenCalledTimes(1);
    expect(mockPatchSettings).toHaveBeenCalledTimes(3);
  });

  it("Cancel creates nothing and leaves the change unsaved", async () => {
    mockPatchSettings.mockRejectedValueOnce(NO_ADMIN);
    renderSystem();
    await askToTurnItOn();
    const dialog = await guidedStep();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    expect(mockCreateUser).not.toHaveBeenCalled();
    expect(screen.getByRole("switch", { name: "Authentication" })).toBeChecked(); // still staged, not saved
  });

  it("any other refusal stays a message: no guided step", async () => {
    mockPatchSettings.mockRejectedValueOnce(refusal("auth_secret_insecure", "auth_enabled: MUSIZEER_AUTH_SECRET is too short"));
    renderSystem();

    await askToTurnItOn();

    await waitFor(() => expect(mockToast.error).toHaveBeenCalledWith(expect.stringContaining("Authentication can't be turned on")));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("with an administrator already there, saving just turns it on", async () => {
    mockPatchSettings.mockResolvedValueOnce({ auth_enabled: true });
    renderSystem();

    await askToTurnItOn();

    await waitFor(() => expect(mockToast.success).toHaveBeenCalledWith("Settings saved"));
    expect(mockPatchSettings).toHaveBeenCalledWith({ auth_enabled: true });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(mockCreateUser).not.toHaveBeenCalled();
  });
});
