import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  Outlet,
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  redirect,
} from "@tanstack/react-router";
import { AxiosError } from "axios";
import { I18nProvider } from "@/i18n";
import { NotFoundPage } from "@/routes/not-found";
import { RouteError } from "./RouteError";

let broken = true;

// React logs every error a boundary catches: expected here, and noisy.
beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
});

function Boom() {
  if (broken) throw new AxiosError("Network Error", "ERR_NETWORK");
  return <p>recovered</p>;
}

function renderAt(path: string) {
  const root = createRootRoute({
    component: () => (
      <div>
        <nav>shell</nav>
        <Outlet />
      </div>
    ),
  });
  const home = createRoute({ getParentRoute: () => root, path: "/", component: () => <p>home</p> });
  const page = createRoute({ getParentRoute: () => root, path: "/boom", component: Boom });
  const library = createRoute({
    getParentRoute: () => root,
    path: "/library",
    beforeLoad: () => {
      throw redirect({ to: "/library/saved" });
    },
  });
  const saved = createRoute({ getParentRoute: () => root, path: "/library/saved", component: () => <p>saved</p> });
  const router = createRouter({
    routeTree: root.addChildren([home, page, library, saved]),
    history: createMemoryHistory({ initialEntries: [path] }),
    defaultErrorComponent: RouteError,
    defaultNotFoundComponent: NotFoundPage,
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <I18nProvider>
        <RouterProvider router={router} />
      </I18nProvider>
    </QueryClientProvider>,
  );
  return router;
}

describe("route error boundary", () => {
  it("a failing page shows the error inside the shell, classified", async () => {
    broken = true;
    renderAt("/boom");

    expect(await screen.findByRole("alert")).toHaveTextContent("The server did not answer.");
    expect(screen.getByText("shell")).toBeInTheDocument();
  });

  it("try again re-renders the page", async () => {
    broken = true;
    renderAt("/boom");
    await screen.findByRole("alert");

    broken = false;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("recovered")).toBeInTheDocument();
  });
});

describe("unknown paths", () => {
  it("show a 404 page with a way home", async () => {
    renderAt("/discover");

    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByText("There is nothing at /discover.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Home" })).toHaveAttribute("href", "/");
  });

  it("/library goes to the saved library", async () => {
    const router = renderAt("/library");

    expect(await screen.findByText("saved")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/library/saved");
  });
});
