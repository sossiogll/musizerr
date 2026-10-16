import { defineConfig, devices } from "@playwright/test";

/**
 * Smoke suite against the running dev stack (docker-compose.dev.yml), run in
 * the Playwright container by e2e/run.sh. See e2e/README.md.
 */
export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "./test-results",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://frontend:5173",
    trace: "retain-on-failure",
    locale: "en-US",
    timezoneId: "Europe/Rome",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // The audio starts after an asynchronous retry: no user gesture then.
        launchOptions: { args: ["--autoplay-policy=no-user-gesture-required"] },
      },
    },
  ],
});
