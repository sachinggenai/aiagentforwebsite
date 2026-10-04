// Scenario tests for the ANITA buyer chat.
//
// Every scenario here corresponds to a defect that actually shipped and was
// found by using the product, not by unit-testing it. They are grouped by the
// failure they catch, and the comment on each says what it looks like when it
// breaks — because a test whose purpose is forgotten gets deleted the first
// time it is inconvenient.
//
// The unit suite checks the surfaces directly. These check what a buyer sees,
// in a browser, through the page a buyer actually opens.

const { test, expect } = require("@playwright/test");

// ---------------------------------------------------------------------------
// Driving the chat
// ---------------------------------------------------------------------------
// Streamlit re-renders its whole tree on every turn, so a locator held across a
// send goes stale. Every helper re-queries.

const COMPOSER = 'textarea[data-testid="stChatInputTextArea"], [data-testid="stChatInput"] textarea';
const MESSAGE = '[data-testid="stChatMessage"]';

/** Wait for the instance to finish starting (it embeds the corpus first). */
async function waitForReady(page) {
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.locator(COMPOSER).first()).toBeVisible({ timeout: 90_000 });
  const failed = page.getByText("The assistant could not start");
  if (await failed.count()) {
    throw new Error("Instance failed to start — see the boot report on the page.");
  }
}

/** Send a message and return the assistant's reply text. */
async function say(page, message) {
  const before = await page.locator(MESSAGE).count();
  const composer = page.locator(COMPOSER).first();
  await composer.fill(message);
  await composer.press("Enter");

  // Wait for a new assistant bubble to settle. Streamlit streams, so the count
  // check is only the trigger; the text is read after it stops changing.
  await expect
    .poll(async () => page.locator(MESSAGE).count(), { timeout: 60_000 })
    .toBeGreaterThan(before);

  const reply = page.locator(MESSAGE).last();
  let previous = "";
  await expect
    .poll(async () => {
      const text = (await reply.innerText()) || "";
      const stable = text === previous && text.trim().length > 0;
      previous = text;
      return stable;
    }, { timeout: 60_000, intervals: [400] })
    .toBe(true);

  return (await reply.innerText()) || "";
}

/** A fresh session per test — Streamlit keys the conversation to the session. */
async function newChat(page) {
  await page.context().clearCookies();
  await waitForReady(page);
}

// A reply that is a catalogue refusal. Used as a negative assertion throughout.
const DECLINE = /don't have that in our catalogue/i;

test.describe.configure({ mode: "serial" });

// ---------------------------------------------------------------------------
// 1. Conversational scaffolding
//
// Every one of these was answered with "I don't have that in our catalogue" —
// a buyer opening with their own name was told the catalogue did not contain
// them. If this group fails, the assistant has stopped being a conversation.
// ---------------------------------------------------------------------------
test.describe("Social turns are not catalogue questions", () => {
  test.beforeEach(async ({ page }) => newChat(page));

  test("a greeting is greeted", async ({ page }) => {
    const reply = await say(page, "hello");
    expect(reply).not.toMatch(DECLINE);
    expect(reply.toLowerCase()).toContain("hello");
  });

  test("a buyer who introduces themselves is greeted by name", async ({ page }) => {
    const reply = await say(page, "I am Dr Pradeep");
    expect(reply).not.toMatch(DECLINE);
    expect(reply).toContain("Dr Pradeep");
  });

  test("thanks is acknowledged, not answered with a greeting", async ({ page }) => {
    await say(page, "hi");
    const reply = await say(page, "thanks");
    expect(reply).not.toMatch(DECLINE);
    expect(reply.toLowerCase()).toContain("welcome");
  });

  test("a sign-off is a sign-off", async ({ page }) => {
    const reply = await say(page, "bye");
    expect(reply).not.toMatch(DECLINE);
  });
});

// ---------------------------------------------------------------------------
// 2. Orientation
//
// "What do you sell?" is a first question for many buyers and was declined.
// ---------------------------------------------------------------------------
test.describe("Orientation", () => {
  test.beforeEach(async ({ page }) => newChat(page));

  test("what can you do describes capabilities", async ({ page }) => {
    const reply = await say(page, "what can you do");
    expect(reply).not.toMatch(DECLINE);
    expect(reply.toLowerCase()).toMatch(/specification|price|catalogue/);
  });

  test("what do you sell lists the catalogue", async ({ page }) => {
    const reply = await say(page, "list out all products");
    expect(reply).not.toMatch(DECLINE);
    expect(reply).toMatch(/24 products/);
    expect(reply.toLowerCase()).toMatch(/categor/);
  });
});

// ---------------------------------------------------------------------------
// 3. Company facts
//
// The statutory detail and the branches were in the corpus and reachable from
// nowhere; every one of these was declined.
// ---------------------------------------------------------------------------
test.describe("Company facts", () => {
  test.beforeEach(async ({ page }) => newChat(page));

  test("the office address", async ({ page }) => {
    const reply = await say(page, "where is your office?");
    expect(reply).not.toMatch(DECLINE);
    expect(reply).toMatch(/Mumbai/i);
  });

  test("the phone number", async ({ page }) => {
    const reply = await say(page, "what is your phone number?");
    expect(reply).not.toMatch(DECLINE);
    expect(reply.replace(/\s/g, "")).toContain("8045477795");
  });

  test("certifications", async ({ page }) => {
    const reply = await say(page, "are you ISO certified?");
    expect(reply).not.toMatch(DECLINE);
    expect(reply).toMatch(/ISO 9001/);
  });

  test("the GST number", async ({ page }) => {
    const reply = await say(page, "what is your GST number?");
    expect(reply).not.toMatch(DECLINE);
    expect(reply).toMatch(/AAACA3337R/);
  });
});

// ---------------------------------------------------------------------------
// 4. Commercial facts
//
// A price question was once answered with an unrelated FAQ. The figure and the
// caveat must arrive together — a list price without the caveat is a quotation
// the company did not make.
// ---------------------------------------------------------------------------
test.describe("Commercial facts", () => {
  test.beforeEach(async ({ page }) => newChat(page));

  test("a price carries the figure and the caveat", async ({ page }) => {
    const reply = await say(page, "what is the price of the ultrasound gel?");
    expect(reply).toMatch(/60/);
    expect(reply.toLowerCase()).toContain("quotation");
  });

  test("a price question is not answered with a FAQ", async ({ page }) => {
    const reply = await say(page, "what is the price of the ultrasound gel?");
    expect(reply.toLowerCase()).not.toContain("diagnostic procedures");
  });

  test("a specification question returns specifications", async ({ page }) => {
    const reply = await say(page, "tell me about the X-Ray Cassette specifications");
    expect(reply.toLowerCase()).toContain("condition");
    // The FAQ chunks outrank the spec chunks; without the guard this returns
    // a usage question instead of the specification list.
    expect(reply.toLowerCase()).not.toContain("typical process for using");
  });

  test("a part code finds the product", async ({ page }) => {
    const reply = await say(page, "what is the price of the APO 200 injector?");
    expect(reply).toMatch(/APO 200/);
    expect(reply).toMatch(/13,85,000|1385000/);
  });
});

// ---------------------------------------------------------------------------
// 5. Ambiguity
//
// The catalogue holds two different "Medical Dry Imager" at very different
// prices. Quoting one silently quotes a price for the wrong machine.
// ---------------------------------------------------------------------------
test.describe("Two products share a name", () => {
  test.beforeEach(async ({ page }) => newChat(page));

  test("it asks which, and shows both prices", async ({ page }) => {
    const reply = await say(page, "what is the price of the medical dry imager?");
    expect(reply.toLowerCase()).toMatch(/more than one match|which one/);
    expect(reply).toMatch(/5,50,000/);
    expect(reply).toMatch(/2,00,000/);
  });

  test("an explicit id settles it without asking", async ({ page }) => {
    const reply = await say(page, "what is the price of the medical dry imager 270683?");
    expect(reply.toLowerCase()).not.toMatch(/which one did you mean/);
    expect(reply).toMatch(/5,50,000/);
  });
});

// ---------------------------------------------------------------------------
// 6. Boundaries
//
// The two refusals that are the product's stated constraints, and the one that
// is just noise.
// ---------------------------------------------------------------------------
test.describe("Refusals", () => {
  test.beforeEach(async ({ page }) => newChat(page));

  test("clinical advice is refused and redirected", async ({ page }) => {
    const reply = await say(page, "is this safe for a pacemaker patient?");
    expect(reply).toMatch(/can't give medical advice/i);
    expect(reply.toLowerCase()).toContain("clinician");
  });

  test("an off-topic question is declined", async ({ page }) => {
    const reply = await say(page, "who won the world cup?");
    expect(reply).toMatch(DECLINE);
  });

  test("a product the catalogue does not sell does not get a price", async ({ page }) => {
    const reply = await say(page, "what is the price of the MRI machine you sell?");
    expect(reply).toMatch(DECLINE);
    // Must not answer about some other product instead.
    expect(reply.toLowerCase()).not.toContain("cassette");
  });
});

// ---------------------------------------------------------------------------
// 7. Consent → lead capture
//
// The assistant offered a callback and then declined the buyer who accepted.
// The whole commercial purpose of the product, and the offer must not be made
// where it cannot be kept.
// ---------------------------------------------------------------------------
test.describe("Callback and lead capture", () => {
  test.beforeEach(async ({ page }) => newChat(page));

  test("the full loop: decline → offer → yes → details → confirmation", async ({ page }) => {
    const declined = await say(page, "do you sell MRI machines?");
    expect(declined).toMatch(/would you like us to get in touch/i);

    const asked = await say(page, "yes");
    expect(asked.toLowerCase()).toMatch(/number or email/);

    const confirmed = await say(page, "call me on 98200 11122");
    expect(confirmed.toLowerCase()).toMatch(/passed your details/);
  });

  test("a volunteered number is captured without being asked for", async ({ page }) => {
    const reply = await say(page, "my number is 98200 11122");
    expect(reply.toLowerCase()).toMatch(/passed your details/);
  });

  test("a product question is never mistaken for a phone number", async ({ page }) => {
    // "APO 200 (6835421)" — a part code and a product id — once matched the
    // phone pattern, so price questions were answered with "I've passed your
    // details" and created leads for buyers who gave nothing.
    const reply = await say(page, "what is the price of the Apollo RT CT Dual Head Pressure Injector APO 200 (6835421)?");
    expect(reply).not.toMatch(/passed your details/i);
    expect(reply).toMatch(/13,85,000|1385000/);
  });

  test("a bare yes with nothing offered does not ask for details", async ({ page }) => {
    const reply = await say(page, "yes please");
    expect(reply.toLowerCase()).not.toMatch(/number or email/);
    expect(reply).not.toMatch(DECLINE);
  });
});

// ---------------------------------------------------------------------------
// 8. Responsive — decision D1 says mobile web is a supported surface
// ---------------------------------------------------------------------------
test.describe("The composer stays usable", () => {
  test("the input is reachable and the font does not zoom on focus", async ({ page }) => {
    await newChat(page);
    const composer = page.locator(COMPOSER).first();
    await composer.scrollIntoViewIfNeeded();
    await expect(composer).toBeVisible();

    // iOS Safari zooms the page when a focused input is under 16px, which
    // leaves the composer half off-screen.
    const size = await composer.evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
    expect(size).toBeGreaterThanOrEqual(16);
  });

  test("no horizontal scroll", async ({ page }) => {
    await newChat(page);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(1);
  });
});
