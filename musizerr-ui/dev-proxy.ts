import type { ProxyOptions } from "vite";

/**
 * The dev server's `/api` proxy to the backend.
 *
 * Vite pipes the backend's response into the browser's (`proxyRes.pipe(res)`)
 * and ends the browser's response only when the backend's ends. A backend
 * that cuts a body short after the headers (a 200 whose body never comes
 * whole) left the browser's request open forever: six of them fill Chrome's
 * per-origin connection pool, every later request — `/api/health` included —
 * queues behind them, and the app shows itself offline until Vite restarts.
 *
 * Here a backend response that ends without completing destroys the
 * browser's response, so the browser sees a failed request and frees the
 * connection; a backend that goes silent is cut after `idleTimeoutMs`.
 */

/** Longest silence of the backend inside one response (the progress stream
 * sends a keepalive every 15 s). */
export const PROXY_IDLE_TIMEOUT_MS = 60_000;

export function apiProxy(target: string, { idleTimeoutMs = PROXY_IDLE_TIMEOUT_MS } = {}): ProxyOptions {
  return {
    target,
    rewrite: (path) => path.replace(/^\/api/, ""),
    changeOrigin: true,
    // The backend socket, idle this long, is aborted (then handled below).
    proxyTimeout: idleTimeoutMs,
    // The browser socket: never left hanging longer than the backend one.
    timeout: idleTimeoutMs * 2,
    configure(proxy) {
      proxy.on("proxyRes", (proxyRes, _req, res) => {
        const abort = () => {
          if (!res.writableEnded && !res.destroyed) res.destroy();
        };
        proxyRes.on("aborted", abort);
        proxyRes.on("error", abort);
        proxyRes.on("close", () => {
          if (!proxyRes.complete) abort();
        });
      });
    },
  };
}
