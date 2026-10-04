// Browser-mic voice scenarios — US-003 (transport) and US-008 (client).
//
// These run against the FastAPI-served buyer page, which is where the voice
// client lives; the main scenario suite drives the Streamlit surface and stays
// as it is. `run_voice.sh` starts the instance and points
// ANITA_VOICE_E2E_URL at it; without that variable this file skips, so a plain
// `npx playwright test` against Streamlit does not fail on a missing instance.
//
// What is asserted here is the client contract, not the answer quality: the
// audio content of a fake device is a test tone, and asking Whisper to
// transcribe it would measure the tone. The path under test is
// gesture → ticket → socket → capture → state — the part that was missing
// entirely before US-008 (the page recorded and discarded the audio).

const { test, expect } = require("@playwright/test");

const VOICE_URL = process.env.ANITA_VOICE_E2E_URL || "";
test.skip(!VOICE_URL, "set ANITA_VOICE_E2E_URL (see run_voice.sh)");

// `channel: "chromium"` selects the full browser, not Playwright's default
// headless *shell* — the shell ships without a media stack, and getUserMedia
// there fails with `NotSupportedError: Not supported` regardless of the fake
// device flag or granted permission. Scoped to this file so the Streamlit
// scenario suite keeps running on the default.
test.use({ baseURL: VOICE_URL, channel: "chromium" });

const MIC = "#mic";
const STATUS = "#voice-status";

test.describe.configure({ mode: "serial" });

// ---------------------------------------------------------------------------
// 1. The fallback paths — every failure leaves text chat usable (TAC-6/TAC-7)
// ---------------------------------------------------------------------------
test.describe("Voice degrades without taking the page down", () => {
  test("a denied microphone offers text chat and the phone number", async ({ page }) => {
    await page.goto("/");
    const status = page.locator(STATUS);
    await expect(status).toHaveText(/Voice off/i);

    await page.locator(MIC).click();

    // Not an error state: a sentence, the text chat, and (when the server
    // supplied one) the callback number.
    await expect(status).toHaveAttribute("data-state", "degraded", { timeout: 30_000 });
    await expect(status).toHaveText(/microphone|call us/i);

    // The buyer can still get an answer the other way — the voice path is
    // never the only path.
    await page.locator("#text").fill("hello");
    await page.locator("#send").click();
    const reply = page.locator(".msg.agent").last();
    await expect(reply).not.toHaveText("…", { timeout: 60_000 });
    await expect(reply).not.toHaveText(/something went wrong/i);
  });
});

// ---------------------------------------------------------------------------
// 2. The live path — gesture → ticket → socket → capture → stop
// ---------------------------------------------------------------------------
test.describe("A granted microphone opens the voice session", () => {
  test("ticket, socket, listening, and a clean stop", async ({ page, context }) => {
    await context.grantPermissions(["microphone"], { origin: VOICE_URL });

    const sockets = [];
    page.on("websocket", (ws) => sockets.push(ws.url()));

    await page.goto("/");
    const ticket = page.waitForResponse((res) => res.url().includes("/api/voice/ticket"));
    await page.locator(MIC).click();

    // The ticket is the admission forecast; 200 with a signed token.
    expect((await ticket).status()).toBe(200);

    const status = page.locator(STATUS);
    // Ready arrives from the server once the socket is open and the session
    // announced itself. `listening` is the evidence the whole chain ran.
    await expect(status).toHaveAttribute("data-state", /listening|thinking|speaking/,
      { timeout: 45_000 });
    expect(sockets.some((url) => url.includes("/voice/browser"))).toBeTruthy();

    // Stop. If a turn started in the meantime the first click interrupts and
    // the second stops — either way the session must reach `ended`.
    for (let press = 0; press < 3; press += 1) {
      if ((await status.getAttribute("data-state")) === "ended") break;
      await page.locator(MIC).click();
      await page.waitForTimeout(500);
    }
    await expect(status).toHaveAttribute("data-state", "ended");
    await expect(status).toHaveText(/ended/i);
  });
});
