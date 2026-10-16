import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  useSearch,
} from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import { viewerStatus } from "@/test/viewer";
import { useCapabilities, type Capability } from "@/hooks/useCapabilities";
import { RequireCapability } from "./RequireCapability";
import type { AuthConfig, ViewerMode } from "@/types";

let status: AuthConfig | undefined;
vi.mock("@/contexts/AuthContext", () => ({ useAuth: () => ({ authConfig: status }) }));

function LoginProbe() {
  const { redirect } = useSearch({ strict: false }) as { redirect?: string };
  return <p>login page, back to {redirect}</p>;
}

function renderGuarded(capability: Capability | Capability[], mode: ViewerMode | null) {
  status = mode ? viewerStatus(mode) : undefined;
  const root = createRootRoute();
  const page = createRoute({
    getParentRoute: () => root,
    path: "/area",
    component: () => (
      <RequireCapability capability={capability}>
        <p>protected</p>
      </RequireCapability>
    ),
  });
  const login = createRoute({ getParentRoute: () => root, path: "/login", component: LoginProbe });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => <p>home</p> });
  const router = createRouter({
    routeTree: root.addChildren([page, login, home]),
    history: createMemoryHistory({ initialEntries: ["/area?tab=x"] }),
  });
  render(
    <I18nProvider>
      <RouterProvider router={router} />
    </I18nProvider>,
  );
}

// The same matrix as docs/permissions.md: which mode may open which area.
const MATRIX: [Capability, Record<ViewerMode, boolean>][] = [
  ["library", { guest: false, user: true, admin: true, open: true }],
  ["download", { guest: false, user: true, admin: true, open: true }],
  ["search", { guest: false, user: true, admin: true, open: true }],
  ["admin", { guest: false, user: false, admin: true, open: true }],
  ["playlists", { guest: false, user: true, admin: true, open: true }],
  ["settings_personal", { guest: false, user: true, admin: true, open: false }],
  ["linked_accounts", { guest: false, user: true, admin: true, open: true }],
];

describe("RequireCapability across the four modes", () => {
  for (const [capability, allowed] of MATRIX) {
    for (const mode of ["guest", "user", "admin", "open"] as ViewerMode[]) {
      it(`${capability} for ${mode}: ${allowed[mode] ? "renders" : "does not render"}`, async () => {
        renderGuarded(capability, mode);
        if (allowed[mode]) {
          expect(await screen.findByText("protected")).toBeInTheDocument();
        } else {
          // A guest goes to sign in; a signed-in viewer is told why.
          await screen.findByText(mode === "guest" ? /login page/ : "Not allowed");
          expect(screen.queryByText("protected")).not.toBeInTheDocument();
        }
      });
    }
  }

  it("a guest is sent to sign in and brought back where they were", async () => {
    renderGuarded("library", "guest");
    expect(await screen.findByText("login page, back to /area?tab=x")).toBeInTheDocument();
  });

  it("fails closed while the viewer is unknown: nothing is allowed", async () => {
    renderGuarded("library", null);
    expect(await screen.findByText("Not allowed")).toBeInTheDocument();
    expect(screen.queryByText("protected")).not.toBeInTheDocument();
  });

  it("any of several capabilities is enough (settings: personal or admin)", async () => {
    renderGuarded(["settings_personal", "admin"], "open");
    expect(await screen.findByText("protected")).toBeInTheDocument();
  });
});

describe("useCapabilities", () => {
  function Probe() {
    const { mode, can } = useCapabilities();
    return <p>{`${mode}:${Object.entries(can).filter(([, v]) => v).map(([k]) => k).join(",")}`}</p>;
  }

  it("is exactly what the server said, nothing derived", () => {
    status = { ...viewerStatus("user"), capabilities: { ...viewerStatus("user").capabilities, download: false } };
    render(<Probe />);
    expect(screen.getByText("user:search,favorite,stream_full,library,playlists,settings_personal,linked_accounts")).toBeInTheDocument();
  });

  it("is empty while unknown", () => {
    status = undefined;
    render(<Probe />);
    expect(screen.getByText("null:")).toBeInTheDocument();
  });
});
