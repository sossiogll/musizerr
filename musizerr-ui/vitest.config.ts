import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

// The zone the date bugs showed in: a time read without its offset is two
// hours off here, so a test on the real API answers catches it.
process.env.TZ = "Europe/Rome";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // dev-proxy.test.ts: the dev server's /api proxy (vite.config.ts), in node.
    include: ["src/**/*.{test,spec}.{ts,tsx}", "dev-proxy.test.ts"],
  },
});
