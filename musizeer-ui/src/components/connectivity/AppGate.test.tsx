import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";
import { I18nProvider } from "@/i18n";
import { authStore } from "@/lib/auth-store";
import { queryKeys } from "@/lib/query-keys";
import { connectivityMock } from "@/test/connectivity-mock";
import { AppGate } from "./AppGate";
import { OfflineBanner } from "./OfflineBanner";
import type { AuthConfig, User } from "@/types";

vi.mock("@/lib/connectivity", async () => (await import("@/test/connectivity-mock")).connectivityModule());

const mockGetAuthConfig = vi.fn();
const mockGetMe = vi.fn();
const mockRefresh = vi.fn();

vi.mock("@/lib/api-client", () => ({
  getAuthConfig: () => mockGetAuthConfig(),
  getMe: () => mockGetMe(),
  refreshAccessToken: () => mockRefresh(),
  login: vi.fn(),
  logoutRequest: vi.fn(),
}));

const status = (auth_enabled: boolean, mode: AuthConfig["mode"] = auth_enabled ? "guest" : "open"): AuthConfig => ({
  auth_enabled,
  registration_open: false,
  mode,
  capabilities: { search: false, download: false, download_plain: false, favorite: false, stream_full: false, library: false, playlists: false, settings_personal: false, linked_accounts: false, admin: mode !== "guest" && mode !== "user" },
  download_policy: null,
});
const alice: User = { id: "u1", username: "alice", email: null, role: "user", is_active: true };

function Viewer() {
  const { user, authState } = useAuth();
  return <p>app:{authState}:{user?.username ?? "nobody"}</p>;
}

function renderGate(queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <AuthProvider>
          <AppGate>
            <OfflineBanner />
            <Viewer />
          </AppGate>
        </AuthProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockGetAuthConfig.mockReset();
  mockGetMe.mockReset();
  mockRefresh.mockReset();
  connectivityMock.reset();
  authStore.clear();
});

describe("AppGate — fail-closed", () => {
  it("renders nothing of the app while the auth state is unknown", () => {
    mockGetAuthConfig.mockReturnValue(new Promise(() => {}));
    renderGate();

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText(/^app:/)).not.toBeInTheDocument();
  });

  it("an unreachable API with nothing saved shows the first-load screen, never the open app", async () => {
    mockGetAuthConfig.mockRejectedValue(new AxiosError("Network Error", "ERR_NETWORK"));
    renderGate();
    act(() => connectivityMock.set({ online: false }));

    expect(await screen.findByText("Musizeer can't reach the server")).toBeInTheDocument();
    expect(screen.queryByText(/^app:/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));
    expect(connectivityMock.checkNow).toHaveBeenCalled();
  });

  it("a failing status request with the API up is not taken as 'sign-in off' either", async () => {
    mockGetAuthConfig.mockRejectedValue(new AxiosError("Bad", "500"));
    renderGate();

    expect(await screen.findByText("Musizeer can't reach the server")).toBeInTheDocument();
    expect(screen.queryByText(/^app:off/)).not.toBeInTheDocument();
  });

  it("sign-in off renders the app once the server says so", async () => {
    mockGetAuthConfig.mockResolvedValue(status(false));
    renderGate();

    expect(await screen.findByText("app:off:nobody")).toBeInTheDocument();
    expect(mockRefresh).not.toHaveBeenCalled();
  });

  it("with a refresh token, waits for the session before rendering: no guest flash", async () => {
    authStore.setRefreshToken("rt");
    mockGetAuthConfig.mockResolvedValue(status(true, "user"));
    let finish!: () => void;
    mockRefresh.mockImplementation(
      () => new Promise((resolve) => {
        finish = () => {
          authStore.setAccessToken("at");
          resolve("at");
        };
      }),
    );
    mockGetMe.mockResolvedValue(alice);
    renderGate();

    await waitFor(() => expect(mockRefresh).toHaveBeenCalled());
    expect(screen.queryByText(/^app:/)).not.toBeInTheDocument();
    act(() => finish());

    await waitFor(() => expect(screen.getByText("app:on:alice")).toBeInTheDocument());
  });

  it("offline with a saved copy: the app renders from it, with the banner", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const savedAt = new Date(2026, 8, 27, 18, 42).getTime();
    queryClient.setQueryData(queryKeys.viewerStatus("session"), status(true, "user"), { updatedAt: savedAt });
    queryClient.setQueryData(queryKeys.me, alice, { updatedAt: savedAt });
    authStore.setRefreshToken("rt");
    mockGetAuthConfig.mockRejectedValue(new AxiosError("Network Error", "ERR_NETWORK"));
    connectivityMock.set({ online: false, nextCheckAt: Date.now() + 8_000 });

    renderGate(queryClient);

    expect(await screen.findByText("app:on:alice")).toBeInTheDocument();
    // The session cannot be restored while offline, and the refresh token is kept.
    expect(mockRefresh).not.toHaveBeenCalled();
    expect(authStore.getRefreshToken()).toBe("rt");
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("API unreachable.");
    const time = new Intl.DateTimeFormat("en", { hour: "2-digit", minute: "2-digit" }).format(savedAt);
    expect(banner).toHaveTextContent(`Showing data saved at ${time}.`);
    expect(banner).toHaveTextContent(/Next attempt in \d+ s\./);
  });

  it("when the API comes back, the session is restored", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    queryClient.setQueryData(queryKeys.viewerStatus("session"), status(true, "user"));
    queryClient.setQueryData(queryKeys.me, alice);
    authStore.setRefreshToken("rt");
    mockGetAuthConfig.mockResolvedValue(status(true, "user"));
    mockGetMe.mockResolvedValue(alice);
    mockRefresh.mockImplementation(async () => {
      authStore.setAccessToken("at");
      return "at";
    });
    connectivityMock.set({ online: false });
    renderGate(queryClient);
    await screen.findByText("app:on:alice");
    expect(mockRefresh).not.toHaveBeenCalled();

    act(() => connectivityMock.set({ online: true }));

    await waitFor(() => expect(mockRefresh).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
