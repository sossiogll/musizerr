import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";
import { apiProxy } from "./dev-proxy";

const API_URL = process.env.VITE_API_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 5173,
    // The e2e suite (e2e/README.md) reaches the dev server from its own
    // container by the compose service name.
    allowedHosts: ["frontend"],
    hmr: {
      protocol: "ws",
      host: "localhost",
      port: 3000,
    },
    proxy: {
      "/api": apiProxy(API_URL),
    },
  },
  build: {
    rollupOptions: {
      output: {
        // The libraries every page needs in long-lived chunks of their own
        // (their hash changes only when they are upgraded); everything else,
        // pages included (router.tsx), is split where it is used.
        manualChunks(id) {
          if (!id.includes("/node_modules/")) return undefined;
          if (/\/node_modules\/(\.pnpm\/[^/]+\/node_modules\/)?(react|react-dom|scheduler)\//.test(id)) return "react-vendor";
          if (/\/@tanstack\/(react-router|router-core|history)\//.test(id)) return "router-vendor";
          if (/\/@tanstack\/(react-query|query-core)\//.test(id)) return "query-vendor";
          return undefined;
        },
      },
    },
  },
});
