import { spawn } from 'node:child_process';
import { webkit } from '../web/node_modules/playwright/index.mjs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, '..');

async function main() {
  console.log('Starting offline demo server...');
  const server = spawn('.venv/bin/python', ['web_launcher.py', '--demo', '--port', '8798', '--no-browser'], {
    cwd: root,
    stdio: ['ignore', 'pipe', 'inherit'],
  });

  let tokenUrl = '';
  server.stdout.on('data', (chunk) => {
    const text = chunk.toString();
    console.log('[DemoServer]', text.trim());
    const match = text.match(/Open the downloader:\s*(http:\/\/127\.0\.0\.1:8798\/#token=[a-zA-Z0-9_-]+)/);
    if (match) {
      tokenUrl = match[1];
    }
  });

  const start = Date.now();
  while (!tokenUrl && Date.now() - start < 15000) {
    await new Promise((r) => setTimeout(r, 200));
  }

  if (!tokenUrl) {
    server.kill();
    throw new Error('Failed to start demo server or obtain token URL');
  }

  console.log('Connecting Playwright to:', tokenUrl);
  const browser = await webkit.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 },
    deviceScaleFactor: 2,
  });
  const page = await context.newPage();

  await page.goto(tokenUrl);
  await page.waitForSelector('.app-shell', { timeout: 10000 });
  await page.waitForTimeout(1000);

  // 1. Expand Animals to show topics
  console.log('Capturing Library view...');
  // Click chat-title for Animals to expand topics
  const animalsButton = page.locator('.chat-title', { hasText: 'Animals' });
  if (await animalsButton.count()) {
    await animalsButton.click();
    await page.waitForTimeout(500);
  }

  // Select Animals checkbox
  const animalsCheckbox = page.locator('input[aria-label="Select Animals"]');
  if (await animalsCheckbox.count()) {
    await animalsCheckbox.check();
    await page.waitForTimeout(500);
  }

  // Click Scan history button to populate inventory counts
  const scanButton = page.locator('button:has-text("Scan history")');
  if (await scanButton.count()) {
    await scanButton.click();
    // Wait for scan to finish in demo
    await page.waitForTimeout(2000);
  }

  // Fill in quantity e.g. 50 images
  const imagesInput = page.locator('input[placeholder="All (200)"], input[placeholder="All"]');
  if (await imagesInput.count()) {
    await imagesInput.first().fill('50');
    await page.waitForTimeout(400);
  }

  await page.screenshot({ path: path.join(root, 'docs/images/web-ui-library.png') });
  console.log('Saved docs/images/web-ui-library.png');

  // Click "Review download"
  console.log('Opening review download modal...');
  const reviewButton = page.locator('button:has-text("Review download")');
  if (await reviewButton.count()) {
    await reviewButton.click();
    await page.waitForTimeout(2000);

    // Capture the modal too if open!
    const modal = page.locator('dialog.modal');
    if (await modal.count()) {
      console.log('Review modal opened, clicking Start downloads...');
      const startButton = page.locator('button:has-text("Start downloads")');
      if (await startButton.count()) {
        await startButton.click();
        await page.waitForTimeout(1000);
      }
    }
  }

  // 2. Switch to Downloads tab
  console.log('Capturing Downloads view...');
  const downloadsNav = page.locator('nav button:has-text("Downloads")');
  await downloadsNav.click();
  // Wait a couple seconds so some transfers are visible in progress and completed
  await page.waitForTimeout(3000);

  await page.screenshot({ path: path.join(root, 'docs/images/web-ui-downloads.png') });
  console.log('Saved docs/images/web-ui-downloads.png');

  // 3. Switch to Settings tab
  console.log('Capturing Settings view...');
  const settingsNav = page.locator('nav button:has-text("Settings")');
  await settingsNav.click();
  await page.waitForTimeout(1000);

  const dirInput = page.locator('input[value*="tgdl-demo"], input[type="text"]').first();
  if (await dirInput.count()) {
    await dirInput.fill('~/Downloads/TelegramMedia');
    await page.waitForTimeout(400);
  }

  await page.screenshot({ path: path.join(root, 'docs/images/web-ui-settings.png') });
  console.log('Saved docs/images/web-ui-settings.png');

  await browser.close();
  server.kill();
  console.log('Screenshots completed successfully!');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
