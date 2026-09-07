// Generates real UI screenshots using synthetic fixtures, then checks key flows.
import { chromium } from 'playwright';
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const root = process.cwd();
const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
await fs.mkdir('.qa/guide', { recursive: true });
await fs.mkdir('parser_app/static/guide', { recursive: true });
const child = spawn(python, ['-m', 'tests.fixture_server'], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
let stderr = '';
child.stderr.on('data', data => { stderr += data.toString(); });
const ready = await new Promise((resolve, reject) => {
  let text = '';
  const timeout = setTimeout(() => { child.kill(); reject(new Error('Fixture startup timed out: ' + stderr)); }, 15000);
  child.stdout.on('data', data => {
    text += data.toString();
    if (text.includes('\n')) { clearTimeout(timeout); try { resolve(JSON.parse(text.split('\n')[0])); } catch (error) { reject(error); } }
  });
  child.once('exit', code => { clearTimeout(timeout); reject(new Error('Fixture exited: ' + code + ' ' + stderr)); });
});
const origin = 'http://127.0.0.1:' + ready.port;
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE || undefined, headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const errors = [], results = [];
const deadline = setTimeout(() => { child.kill(); process.exit(1); }, 120000);
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => { window.showSaveFilePicker = undefined; });
  await page.goto(origin + '/#key=' + ready.key, { waitUntil: 'networkidle' });
  await page.locator('#connectionLabel').waitFor({ state: 'visible' });
  assert.equal(await page.locator('.stats').count(), 0, 'Dashboard statistics must be removed');
  assert.equal(await page.evaluate(() => location.hash), '', 'Launch token must be removed from address');
  assert.equal((await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--gold'))).trim(), '#dfbd72');
  // Poll from Node, not page.waitForFunction: keep the real strict CSP active.
  const until = async (read, expected, label) => {
    const end = Date.now() + 15000;
    let actual;
    do {
      actual = await read();
      if (actual === expected) return;
      await new Promise(resolve => setTimeout(resolve, 75));
    } while (Date.now() < end);
    throw new Error(label + ': expected ' + expected + ', got ' + actual);
  };
  const go = async view => { await page.locator('nav [data-view="' + view + '"]').click(); };
  const shot = async (name, locator) => {
    await page.locator('#toast').evaluate(el => { el.hidden = true; });
    await locator.screenshot({ path: '.qa/guide/' + name + '.png' });
  };
  await until(() => page.locator('#connectionLabel').textContent(), 'Не подключён', 'Initial connection state');
  await go('connection');
  await shot('connect', page.locator('.connection-grid'));
  await page.locator('#apiId').fill('123456');
  await page.locator('#apiHash').fill('a'.repeat(32)); // Deliberately synthetic, never sent to Telegram.
  await page.locator('#configForm [type=submit]').click();
  await page.locator('#phone').fill('+19999999999');
  await page.locator('#phoneForm [type=submit]').click();
  await page.locator('#code').fill('22222');
  await page.locator('#codeForm [type=submit]').click();
  await page.locator('#password').fill('synthetic-fixture-only');
  await page.locator('#passwordForm [type=submit]').click();
  await until(() => page.locator('#connectionLabel').textContent(), 'Telegram подключён', 'Account connection');
  assert.equal(await page.locator('#password').inputValue(), '');
  assert.equal(await page.locator('#apiHash').inputValue(), '');
  results.push('API settings, code and 2FA UI flow; secret inputs cleared');
  await go('overview');
  await page.locator('#sources').fill('@example_channel');
  await page.locator('#include').fill('дизайн, вакансия');
  await page.locator('#exclude').fill('реклама');
  await page.locator('.advanced summary').click();
  await page.locator('#dateFrom').fill('2026-09-01');
  await page.locator('#dateTo').fill('2026-09-07');
  await shot('collect', page.locator('#jobForm').locator('..'));
  await page.locator('#createJob').click();
  await until(() => page.locator('#toast').isVisible(), true, 'Job creation notice');
  await go('jobs');
  await until(() => page.locator('#allJobs .job-card').count(), 4, 'Created job');
  await shot('jobs', page.locator('#allJobs'));
  const queued = page.locator('#allJobs .job-card').filter({ hasText: '@example_channel' }).filter({ has: page.locator('.badge.queued') });
  await queued.getByRole('button', { name: 'Пауза', exact: true }).click();
  await until(() => page.locator('#allJobs .badge.queued').count(), 0, 'Paused queue');
  results.push('Create job, list jobs, pause queued job');
  await go('messages');
  await until(() => page.locator('.message-card').count(), 30, 'First results page');
  assert.equal(await page.evaluate(() => window.parserInjected), undefined);
  await page.locator('#nextPage').click();
  await until(() => page.locator('.message-card').count(), 5, 'Second results page');
  await page.locator('#search').fill('Учебный пример');
  await until(() => page.locator('.message-card').count(), 2, 'Filtered results');
  await shot('messages', page.locator('#view-messages .panel'));
  await page.locator('#exportFormat').selectOption('json');
  const downloadPromise = page.waitForEvent('download');
  await page.locator('#exportButton').click();
  const download = await downloadPromise;
  await download.saveAs('.qa/export.json');
  const exported = JSON.parse(await fs.readFile('.qa/export.json', 'utf8'));
  assert.equal(exported.length, 2);
  results.push('Safe text rendering, pagination, Unicode search, whole-filter JSON export');
  execFileSync(python, ['-c', "from PIL import Image\nfrom pathlib import Path\nfor p in Path('.qa/guide').glob('*.png'):\n im=Image.open(p).convert('RGB'); im.save(Path('parser_app/static/guide')/(p.stem+'.webp'), 'WEBP', quality=88, method=6)"], { cwd: root });
  await go('guide');
  for (const step of await page.locator('.guide-step').all()) {
    await step.evaluate(el => { el.open = true; });
  }
  for (const image of await page.locator('.guide-zoom img').all()) {
    await image.scrollIntoViewIfNeeded();
    await image.evaluate(el => el.decode());
    assert.ok(await image.evaluate(el => el.naturalWidth > 0));
  }
  await page.locator('.guide-zoom').first().click();
  assert.ok(await page.locator('#imageDialog').isVisible());
  await page.locator('#closeImage').click();
  for (const step of await page.locator('.guide-step').all()) await step.evaluate(el => { el.open = false; });
  await page.locator('.guide-step').nth(1).evaluate(el => { el.open = true; });
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({ path: '.qa/guide-desktop.png', fullPage: true });
  results.push('Four bundled guide screenshots decode, enlarged viewer opens/closes');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    for (const view of ['overview', 'messages', 'jobs', 'connection', 'guide']) {
      await go(view);
      await page.waitForTimeout(120);
      if (width === 390) assert.ok(await page.locator('nav').evaluate(el => el.getBoundingClientRect().height < 140), 'Mobile navigation must stay compact');
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow: ' + view + ' @ ' + width);
      await page.locator('#toast').evaluate(el => { el.hidden = true; });
      await page.screenshot({ path: '.qa/' + view + '-' + width + '.png', fullPage: true });
    }
  }
  results.push('All five views fit desktop and 390px mobile');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await go('connection');
  await page.locator('#clearHistory').click();
  assert.ok(await page.locator('#confirmDialog').isVisible());
  await page.screenshot({ path: '.qa/confirm-desktop.png' });
  await page.keyboard.press('Escape');
  assert.ok(!await page.locator('#confirmDialog').isVisible());
  results.push('Destructive action confirmation and Escape dismissal');
  assert.deepEqual(errors, [], 'Browser JavaScript errors');
  await fs.writeFile('.qa/browser-report.json', JSON.stringify({ ok: true, checks: results }, null, 2));
  console.log(JSON.stringify({ ok: true, checks: results }, null, 2));
  // Capture an actual DOM snapshot for independent artifact-design rendering.
  await go('overview');
  const css = await fs.readFile('parser_app/static/styles.css', 'utf8');
  const html = await page.evaluate(css => {
    const clone = document.documentElement.cloneNode(true);
    clone.querySelectorAll('script,link[rel=stylesheet]').forEach(el => el.remove());
    const style = document.createElement('style'); style.textContent = css;
    clone.querySelector('head').append(style);
    clone.querySelectorAll('img').forEach(el => el.removeAttribute('src'));
    return '<!doctype html>\n' + clone.outerHTML;
  }, css);
  await fs.writeFile('.qa/overview-snapshot.html', html);
  await page.close();
} finally {
  clearTimeout(deadline);
  await browser.close();
  child.kill();
}
