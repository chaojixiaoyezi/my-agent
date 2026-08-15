import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });

const errors = [];
page.on('pageerror', err => errors.push(err.message));

await page.goto('http://localhost:3000/settings/tools');
await page.waitForTimeout(1500);

const html = await page.content();
const hasErrorBoundary = html.includes('页面渲染出错');
console.log('Has ErrorBoundary view:', hasErrorBoundary);

if (hasErrorBoundary) {
  const errorText = await page.locator('pre').textContent().catch(() => 'no pre');
  console.log('Error message:', errorText);
}

await page.screenshot({ path: '/tmp/settings-tools-check.png', fullPage: false });
console.log('Screenshot saved');

console.log('Page errors:', errors);
await browser.close();
