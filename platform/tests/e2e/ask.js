const { chromium } = require('@playwright/test');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:8085/', { waitUntil: 'domcontentloaded' });
  const composer = page.locator('textarea[data-testid="stChatInputTextArea"], [data-testid="stChatInput"] textarea').first();
  await composer.waitFor({ state: 'visible', timeout: 90000 });
  await composer.fill('show me the catalogue');
  await composer.press('Enter');
  const msg = page.locator('[data-testid="stChatMessage"]').last();
  let prev = '';
  for (let i = 0; i < 60; i++) {
    await page.waitForTimeout(700);
    const t = (await msg.innerText()) || '';
    if (t === prev && t.trim()) break;
    prev = t;
  }
  console.log('--- ASKED (in the browser, on localhost:8085) ---');
  console.log('  show me the catalogue');
  console.log('--- STREAMLIT REPLIED ---');
  console.log(prev.split('\n').map(l => '  ' + l).join('\n'));
  await page.screenshot({ path: 'catalogue-answer.png', fullPage: true });
  await browser.close();
})();
