// Optional development verification: real Chromium, real local HTTP/SQLite.
// node examples/verify_dashboard.mjs /absolute/path/to/playwright [python3.13]
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createRequire} from "node:module";
import {createInterface} from "node:readline";
import {fileURLToPath} from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const modulePath = process.argv[2];
assert(modulePath, "Pass an existing Playwright 1.61.1 module path; no runtime dependency is installed.");
const require = createRequire(import.meta.url);
const {chromium} = require(path.resolve(modulePath));
assert.equal(require(path.join(path.resolve(modulePath), "package.json")).version, "1.61.1");
const server = spawn(process.argv[3] || "python3.13", ["-m", "tests.browser_fixture"], {cwd: root, stdio: ["pipe", "pipe", "inherit"]});
const lines = createInterface({input: server.stdout})[Symbol.asyncIterator]();
const first = await lines.next();
assert(!first.done, "Fixture server did not start");
const {url} = JSON.parse(first.value);
async function control(command) {
  server.stdin.write(JSON.stringify(command) + "\n");
  assert.equal(JSON.parse((await lines.next()).value).ok, true);
}
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
};
let browser;
try {
  browser = await chromium.launch({headless: true});
  const page = await browser.newPage({viewport: {width: 1440, height: 1100}, locale: "en-US", timezoneId: "UTC"});
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const text = (id) => page.locator(`#${id}`).innerText();
  const ready = () => page.waitForFunction(() => document.getElementById("metrics-status")?.dataset.state === "ready");
  const refresh = async () => { await page.getByRole("button", {name: "Refresh", exact: true}).click(); await ready(); };
  await page.goto(url);
  await ready();
  assert.equal(await text("queued"), "4");
  assert.equal(await text("leased"), "3");
  for (const [id, expected] of Object.entries({"queued-ready": "2", "queued-delayed": "2", "leases-active": "1", "leases-reclaimable": "1", "leases-exhausted": "1"})) assert.equal(await text(id), expected);
  assert.match(await text("oldest-ready"), /^100 seconds since ready$/);
  assert.match(await text("next-retry"), /12:17:20 AM/);
  assert.match(await text("next-expiry"), /12:16:50 AM/);
  assert.match(await page.locator("#operations").innerText(), /next worker claim/i);
  await page.screenshot({path: path.join(root, ".local", "queue-dashboard-desktop.png"), fullPage: true});
  await page.getByRole("button", {name: "ready-old", exact: true}).click();
  await page.waitForFunction(() => document.getElementById("payload").textContent.includes("ready-old"));
  assert.match(await text("payload"), /<img src=x/);
  assert.equal(await page.locator("img").count(), 0);
  assert.equal(await page.evaluate(() => window.pwned), undefined);

  await control({clock: 1100});
  assert.equal(await text("queued-ready"), "2"); // no automatic refresh
  await refresh();
  assert.equal(await text("queued-ready"), "4");
  assert.equal(await text("leases-active"), "0");
  assert.equal(await text("leases-reclaimable"), "2");
  assert.equal(await text("leased"), "3"); // diagnostics never reconcile rows
  assert.match(await text("oldest-ready"), /^200 seconds since ready$/);
  assert.equal(await text("next-retry"), "— No delayed queued jobs");
  assert.equal(await text("next-expiry"), "— No active leases");
  await control({empty: true});
  await refresh();
  assert.equal(await text("queued"), "0");
  assert.equal(await text("oldest-ready"), "— No ready queued jobs");
  await page.waitForFunction(() => document.getElementById("status").textContent.includes("No deliveries"));
  assert.equal(await page.locator("#rows tr").count(), 0);

  await control({mixed: true});
  await page.route("**/api/metrics", (route) => route.fulfill({status: 503, contentType: "application/json", body: JSON.stringify({error: "synthetic <img src=x> failure"})}));
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await page.waitForFunction(() => document.getElementById("metrics-status").dataset.state === "error");
  assert.match(await text("metrics-status"), /Snapshot unavailable/);
  assert.equal(await text("queued"), "—");
  assert.equal(await text("queued-ready"), "—");
  await page.waitForFunction(() => document.querySelectorAll("#rows tr").length === 9);
  assert.equal(await page.locator("img").count(), 0);
  await page.unroute("**/api/metrics");
  await page.route("**/api/jobs?*", (route) => route.fulfill({status: 503, contentType: "application/json", body: '{"error":"synthetic list failure"}'}));
  await refresh();
  assert.equal(await text("queued-ready"), "2");
  await page.waitForFunction(() => document.getElementById("status").textContent.includes("Deliveries unavailable"));
  assert.equal(await page.locator("#rows tr").count(), 0);
  await page.unroute("**/api/jobs?*");
  await refresh();

  // Hold an actual older metrics response; a filter change issues a newer one.
  const captured = deferred(), release = deferred(), fulfilled = deferred();
  let intercepted = false;
  await page.route("**/api/metrics", async (route) => {
    if (intercepted) return route.continue();
    intercepted = true;
    const response = await route.fetch();
    captured.resolve();
    await release.promise;
    await route.fulfill({response});
    fulfilled.resolve();
  });
  await page.getByRole("button", {name: "Refresh", exact: true}).click();
  await captured.promise;
  await control({clock: 1100});
  await page.getByLabel("State", {exact: true}).selectOption("queued");
  await ready();
  assert.equal(await text("queued-ready"), "4");
  release.resolve();
  await fulfilled.promise;
  await page.waitForLoadState("networkidle");
  assert.equal(await text("queued-ready"), "4");
  assert.match(await text("oldest-ready"), /^200 seconds since ready$/);
  assert.equal(await page.locator("#rows tr").count(), 4);
  await page.unroute("**/api/metrics");

  // Preserve the existing independent stale detail guard too.
  const detailCaptured = deferred(), detailRelease = deferred(), detailDone = deferred();
  await page.route("**/api/jobs/1", async (route) => {
    const response = await route.fetch();
    detailCaptured.resolve();
    await detailRelease.promise;
    await route.fulfill({response});
    detailDone.resolve();
  });
  await page.getByRole("button", {name: "ready-old", exact: true}).click();
  await detailCaptured.promise;
  await page.getByRole("button", {name: "ready-exact", exact: true}).click();
  await page.waitForFunction(() => document.getElementById("payload").textContent.includes("ready-exact"));
  detailRelease.resolve();
  await detailDone.promise;
  await page.waitForLoadState("networkidle");
  assert.equal(await text("detail-title"), "ready-exact");
  assert.match(await text("payload"), /ready-exact/);
  await page.unroute("**/api/jobs/1");
  await page.getByLabel("State", {exact: true}).focus();
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.id), "refresh");
  await page.keyboard.press("Enter");
  await ready();
  await page.waitForFunction(() => !document.getElementById("refresh").disabled);
  assert.equal(await page.evaluate(() => document.activeElement.id), "refresh");
  await page.setViewportSize({width: 390, height: 844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  assert.equal(await page.getByLabel("State", {exact: true}).isVisible(), true);
  await page.screenshot({path: path.join(root, ".local", "queue-dashboard-mobile.png"), fullPage: true});
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({chromium: "ok", real_http: true, logical_clock: true, mixed_empty_refresh: true, independent_failures: true, stale_metrics_and_detail: true, keyboard_mobile: true, injected_elements: 0, page_errors: 0}));
} finally {
  if (browser) await browser.close();
  server.stdin.end();
  await new Promise((resolve) => server.once("exit", resolve));
}
