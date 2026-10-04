const { chromium } = require('@playwright/test');
(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  await p.goto('http://127.0.0.1:8085/', { waitUntil: 'domcontentloaded' });
  const c = p.locator('textarea[data-testid="stChatInputTextArea"], [data-testid="stChatInput"] textarea').first();
  await c.waitFor({ state: 'visible', timeout: 90000 });
  for (const q of ['hello', 'show me the catalogue', 'who won the world cup?']) {
    const before = await p.locator('[data-testid="stChatMessage"]').count();
    await c.fill(q); await c.press('Enter');
    await p.waitForTimeout(1200);
    for (let i = 0; i < 50; i++) {
      await p.waitForTimeout(600);
      if (await p.locator('[data-testid="stChatMessage"]').count() > before + 1) break;
    }
    const t = await p.locator('[data-testid="stChatMessage"]').last().innerText();
    console.log('Q: ' + q); console.log(t.split('\n').filter(Boolean).map(l => '   ' + l).join('\n')); console.log();
  }
  await b.close();
})();
