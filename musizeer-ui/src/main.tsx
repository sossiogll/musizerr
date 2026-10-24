import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { Toaster } from "sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { I18nProvider } from "@/i18n";
import { startConnectivityMonitor } from "@/lib/connectivity";
import { createQueryClient } from "@/lib/query-client";
import { persistQueryCache, restoreQueryCache } from "@/lib/query-persistence";
import { preloadPages, router } from "./router";
import "./index.css";

const queryClient = createQueryClient();
// The saved copy first, so a reload during an outage still has data; then
// keep it current, and let /health decide whether the API is reachable.
restoreQueryCache(queryClient);
persistQueryCache(queryClient);
startConnectivityMonitor();

// The other pages' chunks, once the first page has had its turn.
const idle = window.requestIdleCallback ?? ((cb: () => void) => window.setTimeout(cb, 2_000));
idle(() => void preloadPages());

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <I18nProvider>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <RouterProvider router={router} />
            <Toaster richColors position="bottom-right" />
          </TooltipProvider>
        </QueryClientProvider>
      </I18nProvider>
    </ThemeProvider>
  </StrictMode>,
);
