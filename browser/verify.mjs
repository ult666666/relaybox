// Installed-wheel Chromium check for the RelayBox dashboard. Development tooling only:
// it adds no product route, contacts no external service and uses synthetic data.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createHash} from "node:crypto";
import {mkdtemp, mkdir, writeFile, rm} from "node:fs/promises";
import {createRequire} from "node:module";
import {tmpdir} from "node:os";
import {createInterface} from "node:readline";
import {fileURLToPath} from "node:url";
import path from "node:path";
import {chromium} from "playwright";

assert.equal(process.versions.node.split(".")[0], "24", "Use Node 24");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const output = path.resolve(process.env.RELAYBOX_BROWSER_OUTPUT || path.join(root, ".local", "browser"));
const widths = [1440, 1280, 390, 320];
await mkdir(output, {recursive: true});
// Remove only this check's own previous outputs so stale files never look current.
await Promise.all(["receipt.json", ...widths.map((width) => `synthetic-${width}.png`)]
  .map((name) => rm(path.join(output, name), {force: true})));
const scratch = await mkdtemp(path.join(tmpdir(), "relaybox-browser-process-"));
const python = process.argv[2] || process.env.RELAYBOX_PYTHON || "python";
const server = spawn(python, ["-I", path.join(root, "browser", "fixture.py")], {
  cwd: scratch, stdio: ["pipe", "pipe", "pipe"],
});
let stderr = "", spawnError;
server.stderr.setEncoding("utf8").on("data", (chunk) => { stderr += chunk; });
server.on("error", (error) => { spawnError = error; });
const exited = new Promise((resolve) => server.once("close", (code, signal) => resolve({code, signal})));
const reader = createInterface({input: server.stdout});
const lines = reader[Symbol.asyncIterator]();
function bounded(promise, ms, label) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out`)), ms);
  })]).finally(() => clearTimeout(timer));
}
async function line() {
  const result = await bounded(lines.next(), 10000, "Fixture response");
  assert(!result.done, `Fixture stopped: ${spawnError || stderr}`);
  return JSON.parse(result.value);
}
async function control(command) {
  server.stdin.write(JSON.stringify(command) + "\n");
  assert.deepEqual(await line(), {ok: true});
}
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
};
let browser;
const releases = [];
const lateErrors = [];
// A route handler can still be settling when cleanup closes the browser after a
// failure; record such errors as failures instead of crashing before the receipt.
process.on("unhandledRejection", (error) => { lateErrors.push(String(error)); process.exitCode = 1; });
const require = createRequire(import.meta.url);
const receipt = {synthetic: true, status: "FAIL", playwright: require("playwright/package.json").version, actual: {}, controlled: {}};
try {
  await bounded((async () => {
    const fixture = await line();
    receipt.runtime = fixture;
    browser = await chromium.launch({headless: true}); // Missing Chromium is a hard failure.
    receipt.chromium = browser.version();
    receipt.node = process.versions.node;
    const errors = [];
    const watch = (target) => {
      target.on("pageerror", (error) => errors.push(error.message));
      target.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      target.setDefaultTimeout(5000);
      target.setDefaultNavigationTimeout(10000);
      return target;
    };
    const page = watch(await browser.newPage({viewport: {width: 1440, height: 1100}, locale: "en-US", timezoneId: "UTC"}));
    const text = (id) => page.locator(`#${id}`).innerText();
    const idle = (target = page) => target.waitForFunction(() => document.getElementById("refresh")?.getAttribute("aria-busy") === "false");
    const refresh = async () => { await page.getByRole("button", {name: "Refresh", exact: true}).click(); await idle(); };
    const fits = () => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);

    // Actual installed HTTP, assets and synthetic rows at the supplied clock.
    await page.goto(fixture.url);
    await idle();
    const served = await (await page.request.get(`${fixture.url}/assets/app.js`)).body();
    receipt.actual.app_js_sha256 = createHash("sha256").update(served).digest("hex");
    const metrics = await (await page.request.get(`${fixture.url}/api/metrics`)).json();
    assert.equal(metrics.snapshot_at, 1792785600.25);
    assert.equal(metrics.oldest_queued_ready_wait_seconds, 4.625); // Exact API value stays exact.
    assert.deepEqual(metrics.counts, {queued: 4, leased: 3, succeeded: 1, dead_letter: 1});
    for (const [id, want] of Object.entries({queued: "4", leased: "3", succeeded: "1", dead_letter: "1", "queued-ready": "2", "queued-delayed": "2", "leases-active": "1", "leases-reclaimable": "1", "leases-exhausted": "1"})) assert.equal(await text(id), want, id);
    receipt.actual.raw_api_wait_seconds = metrics.oldest_queued_ready_wait_seconds;
    receipt.actual.displayed_wait = await text("oldest-ready");
    // Released 0.4.0 rendered "4.625 seconds since ready" here (the recorded RED).
    assert.equal(await text("oldest-ready"), "About 4.6 seconds since ready", "Installed fractional ready wait should be readable and approximate");
    assert.match(await text("metrics-status"), /2026/);
    assert.match(await text("next-retry"), /2026/);
    assert.match(await text("next-expiry"), /2026/);
    assert.match(await page.locator("#operations").innerText(), /next worker claim/i);
    await page.getByRole("button", {name: "ready-old", exact: true}).click();
    await page.waitForFunction(() => document.getElementById("payload").textContent.includes("ready-old"));
    assert.match(await text("payload"), /<img src=x/);
    assert.equal(await page.locator("img").count(), 0);
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    for (const width of widths) {
      await page.setViewportSize({width, height: width < 500 ? 844 : 1100});
      assert(await fits(), `${width}px page overflow`);
      assert(await page.getByLabel("State", {exact: true}).isVisible());
      assert(await page.getByRole("table").getByText("Latest 100 deliveries", {exact: false}).count());
      await page.screenshot({path: path.join(output, `synthetic-${width}.png`), fullPage: true});
    }
    receipt.actual.viewports = widths;
    // Every later interaction deliberately stays at the narrowest width.
    await page.getByLabel("State", {exact: true}).selectOption("dead_letter");
    await idle();
    assert.equal(await page.locator("#rows tr").count(), 1);
    await page.getByRole("button", {name: "dead", exact: true}).click();
    await page.waitForFunction(() => document.getElementById("payload").textContent.includes('"row": "dead"'));
    await page.getByLabel("State", {exact: true}).selectOption("");
    await idle();
    await page.getByLabel("State", {exact: true}).focus();
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement.id), "refresh");
    await page.keyboard.press("Enter");
    await idle();
    assert.equal(await page.evaluate(() => document.activeElement.id), "refresh");
    await page.keyboard.press("Tab");
    assert.equal(await page.locator("#rows button").first().evaluate((el) => el === document.activeElement), true);
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => !document.getElementById("detail-meta").textContent.startsWith("Loading"));
    assert.deepEqual(errors, []); // Includes console errors before intentional failure probes.
    receipt.actual.filter_payload_keyboard = true;

    await control({advance: 100});
    assert.equal(await text("queued-ready"), "2"); // No automatic refresh.
    await refresh();
    assert.equal(await text("queued-ready"), "4");
    assert.equal(await text("leased"), "3"); // Diagnostics do not reconcile expired rows.
    assert.equal(await text("leases-active"), "0");
    assert.equal(await text("leases-reclaimable"), "2");
    assert.equal(await text("oldest-ready"), "About 1.7 minutes since ready"); // Actual 104.625 seconds.
    assert.equal(await text("next-retry"), "— No delayed queued jobs");
    assert.equal(await text("next-expiry"), "— No active leases");
    await control({empty: true});
    await refresh();
    assert.equal(await text("queued"), "0");
    assert.equal(await text("oldest-ready"), "— No ready queued jobs");
    assert.equal(await page.locator("#rows tr").count(), 0);
    assert.match(await text("status"), /No deliveries/);
    receipt.actual.clock_only_and_empty = true;
    await control({mixed: true});
    await refresh();

    // Controlled metrics-only rendering probes; HTTP/assets/list/detail remain installed.
    assert.equal(page.viewportSize().width, 320);
    let probe = metrics;
    await page.route("**/api/metrics", (route) => route.fulfill({json: probe}));
    const boundaries = [
      [null, "— No ready queued jobs"], [0, "0 seconds since ready"],
      [0.000001, "Less than 0.1 seconds since ready"], [0.099999, "Less than 0.1 seconds since ready"],
      [0.1, "About 0.1 seconds since ready"], [0.96, "About 1 second since ready"],
      [1, "About 1 second since ready"], [1.04, "About 1 second since ready"],
      [1.25, "About 1.3 seconds since ready"], [4.6398069858551025, "About 4.6 seconds since ready"],
      [59.99, "About 60 seconds since ready"], [60, "About 1 minute since ready"],
      [3599, "About 60 minutes since ready"], [3600, "About 1 hour since ready"],
      [5400, "About 1.5 hours since ready"], [86399, "About 24 hours since ready"],
      [86400, "About 1 day since ready"], [129600, "About 1.5 days since ready"],
      [172800, "About 2 days since ready"], [253402300799, "About 2,932,897 days since ready"],
    ];
    for (const [seconds, want] of boundaries) {
      probe = {...metrics, oldest_queued_ready_wait_seconds: seconds};
      await refresh();
      assert.equal(await text("oldest-ready"), want, `controlled seconds=${seconds}`);
    }
    assert(await fits(), "320px page overflow with the longest supported wait");
    receipt.controlled.duration_boundaries = boundaries;
    receipt.controlled.longest_wait_fits_320 = true;
    // Inclusive year-9999 supported API timestamp; browser must render without NaN/epoch fabrication.
    probe = {...metrics, snapshot_at: 253402300799, next_retry_at: 253402300799, next_lease_expiry: 253402300799};
    await refresh();
    for (const id of ["metrics-status", "next-retry", "next-expiry"]) assert.match(await text(id), /9999/);
    receipt.controlled.year_9999_dates = true;
    // Local number formatting is observable in a second real browser page.
    let germanWait = 4.625;
    const german = watch(await browser.newPage({locale: "de-DE", timezoneId: "UTC"}));
    await german.route("**/api/metrics", (route) => route.fulfill({json: {...metrics, oldest_queued_ready_wait_seconds: germanWait}}));
    await german.goto(fixture.url);
    await idle(german);
    assert.equal(await german.locator("#oldest-ready").innerText(), "About 4,6 seconds since ready");
    germanWait = 253402300799;
    await german.getByRole("button", {name: "Refresh", exact: true}).click();
    await idle(german);
    assert.equal(await german.locator("#oldest-ready").innerText(), "About 2.932.897 days since ready");
    await german.close();
    receipt.controlled.de_DE = ["About 4,6 seconds since ready", "About 2.932.897 days since ready"];
    const operationIds = ["queued", "leased", "succeeded", "dead_letter", "queued-ready", "queued-delayed", "leases-active", "leases-reclaimable", "leases-exhausted", "oldest-ready", "next-retry", "next-expiry"];
    const cleared = async (label) => {
      assert.match(await text("metrics-status"), /^Snapshot unavailable: invalid oldest ready wait\./, label);
      assert.equal(await page.locator("#metrics-status").getAttribute("data-state"), "error", label);
      for (const id of operationIds) assert.equal(await text(id), "—", `${label} ${id}`);
      assert.equal(await page.locator("#rows tr").count(), 9, label); // The list section still works.
    };
    for (const invalid of [-1, "NaN", true, undefined]) {
      probe = {...metrics, oldest_queued_ready_wait_seconds: invalid};
      await refresh();
      await cleared(`invalid ${String(invalid)}`);
    }
    await page.unroute("**/api/metrics");
    // 1e999 is valid JSON that parses to Infinity in the browser.
    await page.route("**/api/metrics", (route) => route.fulfill({contentType: "application/json", body: JSON.stringify(metrics).replace('"oldest_queued_ready_wait_seconds":4.625', '"oldest_queued_ready_wait_seconds":1e999')}));
    await refresh();
    await cleared("nonfinite");
    await page.unroute("**/api/metrics");
    receipt.controlled.invalid_wait_clears_metrics = ["negative", "string", "boolean", "omitted", "nonfinite"];

    await page.route("**/api/metrics", (route) => route.fulfill({status: 503, json: {error: "synthetic <img src=x> failure"}}));
    await refresh();
    for (const id of operationIds) assert.equal(await text(id), "—");
    assert.equal(await page.locator("#rows tr").count(), 9);
    assert.equal(await page.locator("img").count(), 0);
    await page.unroute("**/api/metrics");
    await page.route("**/api/jobs?*", (route) => route.fulfill({status: 503, json: {error: "synthetic list failure"}}));
    await refresh();
    assert.equal(await text("queued-ready"), "2");
    assert.equal(await page.locator("#rows tr").count(), 0);
    assert.match(await text("status"), /Deliveries unavailable/);
    await page.unroute("**/api/jobs?*");
    await refresh();
    receipt.controlled.independent_failures = true;

    // Hold actual responses (not substituted data) to exercise generation guards.
    // Count settled JSON reads per URL: the app's handler for a body runs in the
    // same microtask chain, so a moved counter means the late response was handled.
    await page.evaluate(() => {
      const json = Response.prototype.json;
      window.relayboxSettled = {};
      Response.prototype.json = function () {
        const url = this.url;
        return json.call(this).finally(() => { window.relayboxSettled[url] = (window.relayboxSettled[url] || 0) + 1; });
      };
    });
    const handleLate = async (release, url) => {
      const before = await page.evaluate((key) => window.relayboxSettled[key] || 0, url);
      release.resolve();
      await page.waitForFunction(([key, count]) => (window.relayboxSettled[key] || 0) > count, [url, before]);
    };
    const hold = async (pattern, release) => {
      const captured = deferred();
      let intercepted = false;
      await page.route(pattern, async (route) => {
        if (intercepted) return route.continue();
        intercepted = true;
        const response = await route.fetch();
        captured.resolve();
        await release.promise;
        await route.fulfill({response});
      });
      return {captured: captured.promise}; // Wrapped: an async function would adopt a bare promise.
    };
    for (const [pattern, url] of [["**/api/metrics", "/api/metrics"], ["**/api/jobs?*", "/api/jobs?limit=100"]]) {
      const release = deferred();
      releases.push(release);
      const {captured} = await hold(pattern, release);
      await page.getByRole("button", {name: "Refresh", exact: true}).click();
      await bounded(captured, 5000, "Older response capture");
      if (pattern.includes("metrics")) await control({advance: 100});
      await page.getByLabel("State", {exact: true}).selectOption("queued");
      await idle();
      assert.equal(await text("queued-ready"), "4");
      assert.equal(await page.locator("#rows tr").count(), 4);
      await bounded(handleLate(release, `${fixture.url}${url}`), 5000, "Older response handling");
      assert.equal(await text("queued-ready"), "4");
      assert.equal(await text("oldest-ready"), "About 1.7 minutes since ready");
      assert.equal(await page.locator("#rows tr").count(), 4);
      await page.unroute(pattern);
      await page.getByLabel("State", {exact: true}).selectOption("");
      await idle();
    }
    const jobs = await (await page.request.get(`${fixture.url}/api/jobs?limit=100`)).json();
    const detail = `/api/jobs/${jobs.jobs.find((job) => job.delivery_id === "ready-old").id}`;
    const release = deferred();
    releases.push(release);
    const {captured} = await hold(`**${detail}`, release);
    await page.getByRole("button", {name: "ready-old", exact: true}).click();
    await bounded(captured, 5000, "Older detail capture");
    await page.getByRole("button", {name: "ready-exact", exact: true}).click();
    await page.waitForFunction(() => document.getElementById("payload").textContent.includes("ready-exact"));
    await bounded(handleLate(release, `${fixture.url}${detail}`), 5000, "Older detail handling");
    assert.equal(await text("detail-title"), "ready-exact");
    assert.match(await text("payload"), /ready-exact/);
    await page.unroute(`**${detail}`);
    receipt.controlled.late_metrics_list_and_detail = true;
    // Intentional 503s generate browser console errors; none may be script errors.
    assert.equal(errors.filter((error) => !error.includes("503")).length, 0, JSON.stringify(errors));

    const nojs = await browser.newContext({javaScriptEnabled: false});
    const shell = await nojs.newPage();
    await shell.goto(fixture.url);
    assert(await shell.getByRole("heading", {name: "The inbox", exact: true}).isVisible());
    assert.equal(await shell.locator("#queued").innerText(), "—");
    assert.equal(await shell.locator("#payload").innerText(), "No delivery selected.");
    assert.equal(await shell.locator("#rows tr").count(), 0);
    await nojs.close();
    receipt.actual.no_js_static_shell_only = true; // Existing UI requires JS for data; no functional fallback claimed.
    receipt.status = "PASS";
  })(), 150000, "Browser verification");
} catch (error) {
  receipt.error = String(error.stack || error);
  process.exitCode = 1;
} finally {
  for (const release of releases) release.resolve();
  if (browser) {
    try {
      await bounded(browser.close(), 10000, "Browser close");
    } catch (error) {
      receipt.browser_close_error = String(error);
      process.exitCode = 1;
    }
  }
  server.stdin.end();
  let close;
  try {
    close = await bounded(exited, 7000, "Fixture shutdown/close/join");
  } catch {
    server.kill("SIGKILL");
    close = await bounded(exited, 5000, "Fixture forced stop");
    process.exitCode = 1;
  }
  receipt.cleanup = close;
  if (close.code !== 0 || stderr) {
    receipt.fixture_stderr = stderr;
    process.exitCode = 1;
  }
  if (lateErrors.length) receipt.late_errors = lateErrors;
  if (process.exitCode) receipt.status = "FAIL";
  reader.close();
  await rm(scratch, {recursive: true, force: true});
  await writeFile(path.join(output, "receipt.json"), JSON.stringify(receipt, null, 2) + "\n");
  console.log(JSON.stringify(receipt));
}
