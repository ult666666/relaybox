"use strict";
const byId = (id) => document.getElementById(id);
let generation = 0;
let detailGeneration = 0;
const labels = {queued: "Queued", leased: "Leased", succeeded: "Succeeded", dead_letter: "Dead letter"};
const date = (seconds) => {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "Unavailable";
  const value = new Date(seconds * 1000);
  return Number.isNaN(value.getTime()) ? "Unavailable" : value.toLocaleString();
};
const operationIds = ["queued-ready", "queued-delayed", "leases-active", "leases-reclaimable", "leases-exhausted", "oldest-ready", "next-retry", "next-expiry"];
// The API keeps exact seconds. The display rounds to at most one decimal place
// in the largest fitting unit, with local number formatting, and says "About".
const amount = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1});
const plural = new Intl.PluralRules("en", {maximumFractionDigits: 1});

function readyWait(seconds) {
  if (seconds === null) return "No ready jobs";
  // Never invent a wait: the caller's error path clears the whole snapshot.
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) throw new Error("invalid oldest ready wait");
  if (seconds === 0) return `${amount.format(0)} seconds since ready`;
  if (seconds < 0.1) return `Less than ${amount.format(0.1)} seconds since ready`;
  const [size, unit] = seconds < 60 ? [1, "second"] : seconds < 3600 ? [60, "minute"] : seconds < 86400 ? [3600, "hour"] : [86400, "day"];
  const value = seconds / size;
  return `About ${amount.format(value)} ${unit}${plural.select(value) === "one" ? "" : "s"} since ready`;
}

function clearMetrics() {
  for (const key of [...Object.keys(labels), ...operationIds]) byId(key).textContent = "—";
}

function renderMetrics(metrics) {
  const wait = readyWait(metrics.oldest_queued_ready_wait_seconds);
  for (const key of Object.keys(labels)) byId(key).textContent = String(metrics.counts[key]);
  for (const [id, key] of Object.entries({"queued-ready": "queued_ready", "queued-delayed": "queued_delayed", "leases-active": "leases_active", "leases-reclaimable": "leases_expired_reclaimable", "leases-exhausted": "leases_expired_exhausted"})) {
    byId(id).textContent = String(metrics[key]);
  }
  byId("oldest-ready").textContent = wait;
  byId("next-retry").textContent = metrics.next_retry_at === null ? "No delayed jobs" : date(metrics.next_retry_at);
  byId("next-expiry").textContent = metrics.next_lease_expiry === null ? "No active leases" : date(metrics.next_lease_expiry);
  byId("metrics-status").textContent = `Snapshot at ${date(metrics.snapshot_at)}, local time. Readiness and expiry are measured at that moment.`;
  byId("metrics-status").dataset.state = "ready";
}

async function readJSON(path) {
  const response = await fetch(path, {cache: "no-store"});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function inspect(id, delivery) {
  const current = ++detailGeneration;
  byId("detail-title").textContent = delivery;
  byId("detail-meta").textContent = "Loading delivery…";
  try {
    const {job} = await readJSON(`/api/jobs/${id}`);
    if (current !== detailGeneration) return;
    byId("payload").textContent = JSON.stringify(job.payload, null, 2);
    byId("detail-meta").textContent = `${labels[job.state]} · ${job.replay_count} ${job.replay_count === 1 ? "replay" : "replays"} · SHA-256 ${job.digest}`;
  } catch (error) {
    if (current !== detailGeneration) return;
    byId("detail-meta").textContent = `Could not load delivery: ${error.message}`;
    byId("payload").textContent = "Payload unavailable.";
  }
}

function renderJobs(jobs) {
  const fragment = document.createDocumentFragment();
  for (const job of jobs) {
    const row = document.createElement("tr");
    const deliveryCell = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "delivery-button";
    button.textContent = job.delivery_id;
    button.addEventListener("click", () => inspect(job.id, job.delivery_id));
    deliveryCell.append(button);
    row.append(deliveryCell);
    const stateCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${job.state}`;
    badge.textContent = labels[job.state];
    stateCell.append(badge);
    row.append(stateCell);
    for (const text of [`${job.attempts} / ${job.max_attempts}`, date(job.created_at), job.last_error || "—"]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.append(cell);
    }
    fragment.append(row);
  }
  byId("rows").replaceChildren(fragment);
  byId("status").textContent = jobs.length ? `${jobs.length} ${jobs.length === 1 ? "delivery" : "deliveries"} shown · list updated ${new Date().toLocaleTimeString()}` : "No deliveries match this state. Send a signed webhook or run the local demo.";
}

// Counts stay hidden until a response arrives, so no stale or invented total shows.
function clearDeliveries() {
  byId("outbound-counts").hidden = true;
  for (const key of Object.keys(labels)) byId(`out-${key}`).textContent = "";
  byId("delivery-rows").replaceChildren();
}

function lastResult(delivery) {
  const status = delivery.last_status === null ? "" : `HTTP ${delivery.last_status}`;
  if (delivery.last_error) return status && !delivery.last_error.startsWith(status) ? `${status} · ${delivery.last_error}` : delivery.last_error;
  return status || (delivery.state === "leased" ? "Attempt in progress" : "Not attempted yet");
}

function nextAttempt(delivery) {
  if (delivery.state === "queued") return date(delivery.available_at);
  if (delivery.state === "leased") return `Lease until ${date(delivery.lease_until)}`;
  return "Not scheduled";
}

function renderDeliveries(data) {
  for (const key of Object.keys(labels)) byId(`out-${key}`).textContent = String(data.counts[key]);
  byId("outbound-counts").hidden = false;
  const fragment = document.createDocumentFragment();
  for (const delivery of data.deliveries) {
    const row = document.createElement("tr");
    // Identifiers stay whole; only the endpoint URL may wrap.
    for (const [text, wrap] of [[delivery.webhook_id, false], [delivery.event_type, false], [delivery.url, true]]) {
      const cell = document.createElement("td");
      const value = document.createElement("code");
      value.className = wrap ? "wrap" : "nowrap";
      value.textContent = text;
      cell.append(value);
      row.append(cell);
    }
    const stateCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${delivery.state}`;
    badge.textContent = labels[delivery.state];
    stateCell.append(badge);
    row.append(stateCell);
    for (const text of [`${delivery.attempts} / ${delivery.max_attempts}`, lastResult(delivery), nextAttempt(delivery)]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      row.append(cell);
    }
    const action = document.createElement("td");
    action.className = "nowrap";
    if (delivery.state === "dead_letter" && data.replay_enabled) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "replay-button";
      button.textContent = "Replay";
      button.setAttribute("aria-label", `Replay ${delivery.webhook_id} to ${delivery.url}`);
      button.addEventListener("click", () => replayDelivery(delivery.id, delivery.webhook_id, button));
      action.append(button);
    } else {
      action.textContent = delivery.state === "dead_letter" ? "Use the CLI" : "Not needed";
    }
    row.append(action);
    fragment.append(row);
  }
  byId("delivery-rows").replaceChildren(fragment);
  const count = data.deliveries.length;
  byId("outbound-status").textContent = count ? `${count} outbound ${count === 1 ? "delivery" : "deliveries"} shown · list updated ${new Date().toLocaleTimeString()}` : "No outbound deliveries match this state. Add a subscription, then publish or relay a message.";
}

async function replayDelivery(id, webhookId, button) {
  button.disabled = true;
  byId("outbound-status").textContent = `Replaying ${webhookId}…`;
  let message;
  try {
    const response = await fetch(`/api/deliveries/${id}/replay`, {method: "POST", headers: {"Content-Type": "application/json"}, cache: "no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Replay failed");
    message = `${webhookId} is queued again with a fresh attempt budget and the same webhook-id. Run relaybox deliver to send it.`;
  } catch (error) {
    message = `Replay failed: ${error.message}`;
  }
  await refresh();
  byId("outbound-status").textContent = message;
  byId("outbound-status").focus(); // The clicked row was re-rendered; keep keyboard focus nearby.
}

async function refresh() {
  const current = ++generation;
  const state = byId("state").value;
  byId("refresh").setAttribute("aria-busy", "true");
  clearMetrics();
  byId("metrics-status").dataset.state = "loading";
  byId("metrics-status").textContent = "Loading inbox snapshot…";
  byId("rows").replaceChildren();
  byId("status").textContent = "Loading deliveries…";
  clearDeliveries();
  byId("outbound-status").textContent = "Loading outbound deliveries…";
  const outboundState = byId("outbound-state").value;
  // Each section can succeed independently. Older responses never replace a
  // newer refresh, and failed sections have no stale values left as current.
  const metrics = readJSON("/api/metrics").then((value) => {
    if (current === generation) renderMetrics(value);
  }).catch((error) => {
    if (current !== generation) return;
    clearMetrics();
    byId("metrics-status").dataset.state = "error";
    byId("metrics-status").textContent = `Snapshot unavailable: ${error.message}. Refresh to try again.`;
  });
  const jobs = readJSON(`/api/jobs?limit=100${state ? `&state=${encodeURIComponent(state)}` : ""}`).then(({jobs}) => {
    if (current === generation) renderJobs(jobs);
  }).catch((error) => {
    if (current !== generation) return;
    byId("rows").replaceChildren();
    byId("status").textContent = `Deliveries unavailable: ${error.message}. Check the local server and refresh.`;
  });
  const deliveries = readJSON(`/api/deliveries?limit=100${outboundState ? `&state=${encodeURIComponent(outboundState)}` : ""}`).then((data) => {
    if (current === generation) renderDeliveries(data);
  }).catch((error) => {
    if (current !== generation) return;
    clearDeliveries();
    byId("outbound-status").textContent = `Outbound deliveries unavailable: ${error.message}. Refresh to retry.`;
  });
  await Promise.allSettled([metrics, jobs, deliveries]);
  if (current === generation) byId("refresh").setAttribute("aria-busy", "false");
}
byId("state").addEventListener("change", refresh);
byId("outbound-state").addEventListener("change", refresh);
byId("refresh").addEventListener("click", refresh);
refresh();
