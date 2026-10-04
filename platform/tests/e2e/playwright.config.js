// Playwright configuration for the ANITA buyer chat.
//
// `baseURL` points at the Streamlit surface by default. Override with
// ANITA_E2E_URL to run the same scenarios against the FastAPI-served page
// instead — the assertions are about answers, not about which server sent them.
//
// Streamlit's first paint embeds the whole corpus (~25s), so the navigation
// timeout is generous and the first test waits for the composer rather than for
// a fixed delay.

const { defineConfig, devices } = require("@playwright/test");

const BASE_URL = process.env.ANITA_E2E_URL || "http://127.0.0.1:8085";

module.exports = defineConfig({
  testDir: ".",
  timeout: 120_000,
  expect: { timeout: 60_000 },
  fullyParallel: false, // One shared instance; parallel chats would interleave.
  workers: 1,
  retries: 0,
  reporter: [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: BASE_URL,
    navigationTimeout: 90_000,
    actionTimeout: 60_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    // Mobile web is a stated platform (decision D1), so it is tested, not
    // assumed — and the composer must stay usable above the on-screen keyboard.
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
});
