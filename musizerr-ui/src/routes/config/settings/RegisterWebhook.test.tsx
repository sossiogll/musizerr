import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { I18nProvider } from "@/i18n";
import type { DelegationWebhookRegistrationResult } from "@/lib/api-client";
import { RegisterWebhook } from "./RegisterWebhook";

const mockRegister = vi.fn();

vi.mock("@/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api-client")>("@/lib/api-client");
  return { ...actual, registerDelegationWebhooks: () => mockRegister() };
});
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const registered = (provider: string): DelegationWebhookRegistrationResult => ({
  provider, label: provider, ok: true, state: "registered", detail: "created",
});
const notSupported = (provider: string): DelegationWebhookRegistrationResult => ({
  provider, label: provider, ok: false, state: "not_supported", detail: "not_supported",
});
const failed = (
  provider: string, error_code: string, error_message: string, error_detail?: string,
): DelegationWebhookRegistrationResult => ({
  provider, label: provider, ok: false, state: "failed", detail: "failed", error_code, error_message, error_detail,
});

async function run(results: DelegationWebhookRegistrationResult[]) {
  mockRegister.mockResolvedValue(results);
  render(
    <I18nProvider>
      <RegisterWebhook />
    </I18nProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Register" }));
  await waitFor(() => expect(mockRegister).toHaveBeenCalled());
}

beforeEach(() => {
  mockRegister.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
});

describe("RegisterWebhook", () => {
  it("says a provider without push registration does not support it, and nothing else", async () => {
    await run([notSupported("plain")]);
    expect(await screen.findByText(/plain: does not support push registration/)).toBeInTheDocument();
    expect(toast.error).toHaveBeenCalledWith("No download provider has a push registration");
  });

  it("shows a refusal with its translated code and the provider's own reason, never 'not supported'", async () => {
    await run([failed("lidarr", "download_provider_failed", "The download provider failed.", "Url: Unable to send test message")]);
    expect(await screen.findByText(/lidarr: The download provider failed\./)).toBeInTheDocument();
    expect(screen.getByText("Url: Unable to send test message")).toBeInTheDocument();
    expect(screen.queryByText(/does not support/)).not.toBeInTheDocument();
    expect(toast.error).toHaveBeenCalledWith("No provider accepted the registration");
  });

  it("says an unreachable external system could not be reached", async () => {
    await run([failed("lidarr", "download_provider_unreachable", "x", "Name or service not known")]);
    expect(await screen.findByText(/lidarr: The download provider could not be reached\./)).toBeInTheDocument();
    expect(screen.getByText("Name or service not known")).toBeInTheDocument();
  });

  it("says a provider that answered nothing did not confirm, not that it is unsupported", async () => {
    await run([failed("silent", "registration_not_confirmed", "x")]);
    expect(await screen.findByText(/silent: The provider did not confirm the registration and gave no reason\./)).toBeInTheDocument();
    expect(screen.queryByText(/does not support/)).not.toBeInTheDocument();
  });

  it("reports one provider registered next to another that failed", async () => {
    await run([registered("a"), failed("b", "download_provider_failed", "x", "no")]);
    expect(await screen.findByText(/a: created/)).toBeInTheDocument();
    expect(screen.getByText(/b: The download provider failed\./)).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("Webhook registration completed");
  });
});
