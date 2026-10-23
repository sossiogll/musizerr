import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { AxiosError, AxiosHeaders, type AxiosResponse } from "axios";
import { viewerStatus } from "@/test/viewer";
import { renderPage } from "@/test/render";
import { RegisterPage } from "./register";

const mockRegister = vi.fn();
const mockLogin = vi.fn();
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ login: mockLogin, authConfig: viewerStatus("guest") }) }));
vi.mock("@/lib/api-client", async () => ({
  ...(await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client")),
  register: (...a: unknown[]) => mockRegister(...a),
}));

function refused(status: number, detail: unknown) {
  const response = { status, data: { detail }, statusText: "", headers: {}, config: { headers: new AxiosHeaders() } } as AxiosResponse;
  return new AxiosError("refused", String(status), undefined, undefined, response);
}

async function submit(password: string, confirm = password) {
  fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "alice" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: password } });
  fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: confirm } });
  fireEvent.click(screen.getByRole("button", { name: "Create account" }));
}

beforeEach(() => {
  mockRegister.mockReset();
  mockLogin.mockReset().mockResolvedValue(undefined);
});

describe("RegisterPage — the password rule", () => {
  const render = () => renderPage(RegisterPage, { path: "/register", otherPaths: ["/"] });

  it("does not send a password shorter than the server accepts", async () => {
    render();

    await submit("short");

    expect(await screen.findByText(/The password is too short/)).toBeInTheDocument();
    expect(screen.getByText(/At least 8 characters/)).toBeInTheDocument();
    expect(mockRegister).not.toHaveBeenCalled();
  });

  it("says the server's refusal in words, from its code", async () => {
    mockRegister.mockRejectedValue(
      refused(422, { error_code: "password_too_short", error_message: "The password must have at least 8 characters.", min_length: 8 }),
    );
    render();

    await submit("long-enough");

    expect(await screen.findByText("That password is too short: it needs at least 8 characters.")).toBeInTheDocument();
    expect(mockLogin).not.toHaveBeenCalled();
  });

  it("shows the server's plain text for a taken username", async () => {
    mockRegister.mockRejectedValue(refused(409, "Username already taken"));
    render();

    await submit("long-enough");

    expect(await screen.findByText("Username already taken")).toBeInTheDocument();
  });

  it("registers and signs in with a password of the minimum length", async () => {
    mockRegister.mockResolvedValue({});
    render();

    await submit("12345678");

    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith("alice", "12345678"));
    expect(mockRegister).toHaveBeenCalledWith("alice", "12345678", undefined);
  });
});
