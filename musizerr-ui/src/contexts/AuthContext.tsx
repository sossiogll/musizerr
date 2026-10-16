import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getAuthConfig,
  getMe,
  login as apiLogin,
  logoutRequest,
  refreshAccessToken,
} from "@/lib/api-client";
import { authStore } from "@/lib/auth-store";
import { useConnectivity } from "@/lib/connectivity";
import { queryKeys, type ViewerScope } from "@/lib/query-keys";
import { clearPersistedCache, setCacheOwner } from "@/lib/query-persistence";
import type { AuthConfig, TokenResponse, User } from "@/types";

/**
 * Whether sign-in is enabled on this server. `unknown` until GET
 * /auth/account/status has answered once (or its saved copy is restored):
 * the app is FAIL-CLOSED, so `unknown` never renders the open app — it shows
 * the loading or offline screen instead (see AppGate).
 */
export type AuthState = "unknown" | "on" | "off";

interface AuthContextValue {
  authState: AuthState;
  /** GET /auth/account/status for the current viewer: mode and capabilities. */
  authConfig: AuthConfig | undefined;
  user: User | null;
  /** The viewer is known: auth state resolved and no session restore pending. */
  ready: boolean;
  /** GET /auth/account/status failed and there is no saved copy of it. */
  statusFailed: boolean;
  isLoading: boolean;
  login: (username: string, password: string) => Promise<void>;
  /** Starts the session of a sign-in made elsewhere (through a provider): the pair the server issued. */
  startSession: (tokens: TokenResponse) => Promise<void>;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue>({
  authState: "unknown",
  authConfig: undefined,
  user: null,
  ready: false,
  statusFailed: false,
  isLoading: true,
  login: async () => {},
  startSession: async () => {},
  logout: async () => {},
  refreshUser: async () => {},
});

const REFRESH_LEAD_MS = 60_000;

function useAccessToken(): string | null {
  return useSyncExternalStore(authStore.subscribe, authStore.getAccessToken, authStore.getAccessToken);
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const { online } = useConnectivity();
  const token = useAccessToken();

  // The viewer's status answers for the bearer token, so it is read in the
  // scope of the credentials the viewer holds (see ViewerScope): a saved
  // guest status never stands in for the session's. With a refresh token but
  // no access token yet, the session's status can only be the saved copy of
  // it (offline): asking now would be answered as a guest, so it waits for
  // the token.
  const hasRefreshToken = authStore.getRefreshToken() !== null;
  const scope: ViewerScope = token !== null || hasRefreshToken ? "session" : "anonymous";
  const viewer = useQuery({
    queryKey: queryKeys.viewerStatus(scope),
    queryFn: getAuthConfig,
    staleTime: 5 * 60 * 1000,
    enabled: token !== null || !hasRefreshToken,
  });
  const authConfig = viewer.data;
  const authState: AuthState = authConfig ? (authConfig.auth_enabled ? "on" : "off") : "unknown";

  // A session's status goes with the session: whoever signs in next, maybe
  // someone else, must not meet it.
  useEffect(() => {
    if (scope === "anonymous") queryClient.removeQueries({ queryKey: queryKeys.viewerStatus("session") });
  }, [scope, queryClient]);

  // Restoring a session from the refresh token: attempted whenever there is
  // a refresh token but no access token yet, sign-in is not known to be off
  // and the API is reachable. An unreachable API leaves the refresh token in
  // place (see refreshAccessToken) and the attempt is repeated when it comes
  // back.
  const [restoring, setRestoring] = useState(false);
  const needsRestore = authState !== "off" && !token && hasRefreshToken;

  useEffect(() => {
    if (!needsRestore || !online) return;
    setRestoring(true);
    // Not tied to this effect's lifetime: the token arriving ends
    // needsRestore (and this effect) before the promise settles.
    void refreshAccessToken().finally(() => setRestoring(false));
  }, [needsRestore, online]);

  // The public providers depend on the bearer token too: ask again whenever
  // a session starts or ends. While offline the saved copy stays.
  const hadToken = useRef(token !== null);
  useEffect(() => {
    const has = token !== null;
    if (has !== hadToken.current) {
      hadToken.current = has;
      void queryClient.invalidateQueries({ queryKey: queryKeys.providersPublic });
    }
  }, [token, queryClient]);

  const me = useQuery({
    queryKey: queryKeys.me,
    queryFn: getMe,
    enabled: authState === "on" && token !== null,
    staleTime: 5 * 60 * 1000,
  });
  // A saved copy of "me" stands in while the session cannot be restored
  // because the API is unreachable; it goes away with the refresh token.
  const user = authState === "on" && (token !== null || hasRefreshToken) ? (me.data ?? null) : null;

  const sessionPending = needsRestore && (online || restoring);
  const ready = authState !== "unknown" && !sessionPending && !(token !== null && me.isPending && online);

  // The offline copy belongs to one viewer.
  useEffect(() => {
    if (!ready) return;
    const owner = authState === "off" ? "open" : (user?.id ?? "guest");
    setCacheOwner(queryClient, owner);
  }, [ready, authState, user?.id, queryClient]);

  // Access-token renewal shortly before it expires.
  const refreshTimerRef = useRef<number | null>(null);
  useEffect(() => {
    if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = null;
    if (!token) return;
    const expiresAt = authStore.getAccessTokenExpiresAt();
    if (!expiresAt) return;
    const delay = Math.max(expiresAt - Date.now() - REFRESH_LEAD_MS, 5_000);
    refreshTimerRef.current = window.setTimeout(() => void refreshAccessToken(), delay);
    return () => {
      if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
    };
  }, [token]);

  const refreshUser = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: queryKeys.me });
  }, [queryClient]);

  const startSession = useCallback(
    async (res: TokenResponse) => {
      // Starts a fresh session generation so a refresh still in flight from
      // a previous session cannot land its tokens on top of this one.
      authStore.beginSession();
      authStore.setRefreshToken(res.refresh_token);
      authStore.setAccessToken(res.access_token);
      await queryClient.fetchQuery({ queryKey: queryKeys.me, queryFn: getMe });
    },
    [queryClient],
  );

  const login = useCallback(
    async (username: string, password: string) => startSession(await apiLogin(username, password)),
    [startSession],
  );

  const logout = useCallback(async () => {
    const rt = authStore.getRefreshToken();
    if (rt) {
      try {
        await logoutRequest(rt);
      } catch {
        // server may have already revoked, or be unreachable
      }
    }
    authStore.clear();
    // Nothing of this session may outlive it, in memory or on disk. The
    // viewer's status is asked again, now as a guest.
    clearPersistedCache();
    queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== queryKeys.viewer[0] });
    await queryClient.invalidateQueries({ queryKey: queryKeys.viewer });
  }, [queryClient]);

  // Permissions are not decided here: see useCapabilities.
  const value = useMemo<AuthContextValue>(() => {
    return {
      authState,
      authConfig,
      user,
      ready,
      statusFailed: viewer.isError && !authConfig,
      isLoading: !ready,
      login,
      startSession,
      logout,
      refreshUser,
    };
  }, [authState, authConfig, user, ready, viewer.isError, login, startSession, logout, refreshUser]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export const useAuth = () => useContext(AuthContext);
