import { defineConfig, devices } from "@playwright/test"

// Two kinds of end-to-end test:
//   e2e/ui     the real built app in a real browser, with the backend API mocked from
//              recorded responses (e2e/fixtures). Fast and deterministic; runs on every PR.
//   e2e/stack  the whole Docker stack, no mocks (needs E2E_BASE_URL and E2E_PASSWORD).
const stack = !!process.env.E2E_BASE_URL
const PORT = 3100

export default defineConfig({
  testDir: stack ? "e2e/stack" : "e2e/ui",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // The real stack's first page loads wait on PSX for price history (cold caches).
  expect: { timeout: stack ? 30_000 : 5_000 },
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  // Serves the production build (`npm run build` first). Not used against the stack.
  webServer: stack ? undefined : {
    command: "npm run e2e:serve",
    url: `http://127.0.0.1:${PORT}/login`,
    env: { PORT: String(PORT), HOSTNAME: "127.0.0.1" },
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
})
