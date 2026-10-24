import type { ReactNode } from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  RouterProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  type AnyRoute,
} from "@tanstack/react-router";
import { I18nProvider } from "@/i18n";
import { QueueProvider } from "@/contexts/QueueContext";
import { TooltipProvider } from "@/components/ui/tooltip";

export interface RenderPageOptions {
  /** The route path the page is mounted at (e.g. "/downloads"). */
  path: string;
  /** The URL to start from; defaults to `path`. */
  url?: string;
  validateSearch?: (search: Record<string, unknown>) => Record<string, unknown>;
  /** Other paths the page may link or navigate to, rendered as their path. */
  otherPaths?: string[];
  /** Mount the live download state (QueueProvider): needed by track rows. */
  queue?: boolean;
}

/**
 * Renders a page inside the app's providers and a memory router, the way
 * the app mounts it. Returns the router too, to assert navigation.
 */
export function renderPage(Page: () => ReactNode, options: RenderPageOptions) {
  const root = createRootRoute();
  const page = createRoute({ getParentRoute: () => root, path: options.path, component: Page, validateSearch: options.validateSearch });
  const others: AnyRoute[] = (options.otherPaths ?? []).map((path) =>
    createRoute({ getParentRoute: () => root, path, component: () => <p>{path}</p> }),
  );
  const router = createRouter({
    routeTree: root.addChildren([page, ...others]),
    history: createMemoryHistory({ initialEntries: [options.url ?? options.path] }),
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const app: ReactNode = <RouterProvider router={router} />;
  const result = render(
    <QueryClientProvider client={queryClient}>
      <I18nProvider>
        <TooltipProvider>{options.queue ? <QueueProvider>{app}</QueueProvider> : app}</TooltipProvider>
      </I18nProvider>
    </QueryClientProvider>,
  );
  return { ...result, router, queryClient };
}
