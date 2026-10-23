"use strict";
const byId = (id) => document.getElementById(id);
let generation = 0;
let detailGeneration = 0;
const labels = {queued: "Queued", leased: "Leased", succeeded: "Succeeded", dead_letter: "Dead letter"};
const date = (seconds) => {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "— Unavailable";
  const value = new Date(seconds * 1000);
  return Number.isNaN(value.getTime()) ? "— Unavailable" : value.toLocaleString();
};
const operationIds = ["queued-ready", "queued-delayed", "leases-active", "leases-reclaimable", "leases-exhausted", "oldest-ready", "next-retry", "next-expiry"];

function clearMetrics() {
  for (const key of [...Object.keys(labels), ...operationIds]) byId(key).textContent = "—";
}

function renderMetrics(metrics) {
  for (const key of Object.keys(labels)) byId(key).textContent = String(metrics.counts[key]);
  for (const [id, key] of Object.entries({"queued-ready": "queued_ready", "queued-delayed": "queued_delayed", "leases-active": "leases_active", "leases-reclaimable": "leases_expired_reclaimable", "leases-exhausted": "leases_expired_exhausted"})) {
    byId(id).textContent = String(metrics[key]);
  }
  byId("oldest-ready").textContent = metrics.oldest_queued_ready_wait_seconds === null ? "— No ready queued jobs" : `${metrics.oldest_queued_ready_wait_seconds} seconds since ready`;
  byId("next-retry").textContent = metrics.next_retry_at === null ? "— No delayed queued jobs" : date(metrics.next_retry_at);
  byId("next-expiry").textContent = metrics.next_lease_expiry === null ? "— No active leases" : date(metrics.next_lease_expiry);
  byId("metrics-status").textContent = `Inbox snapshot at ${date(metrics.snapshot_at)} (local time). Readiness and expiry are measured at this moment.`;
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
    byId("detail-meta").textContent = `${labels[job.state]} · SHA-256 ${job.digest} · ${job.replay_count} explicit replays`;
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

async function refresh() {
  const current = ++generation;
  const state = byId("state").value;
  byId("refresh").disabled = true;
  clearMetrics();
  byId("metrics-status").dataset.state = "loading";
  byId("metrics-status").textContent = "Loading inbox snapshot…";
  byId("rows").replaceChildren();
  byId("status").textContent = "Loading deliveries…";
  // Each section can succeed independently. Older responses never replace a
  // newer refresh, and failed sections have no stale values left as current.
  const metrics = readJSON("/api/metrics").then((value) => {
    if (current === generation) renderMetrics(value);
  }).catch((error) => {
    if (current !== generation) return;
    clearMetrics();
    byId("metrics-status").dataset.state = "error";
    byId("metrics-status").textContent = `Snapshot unavailable: ${error.message}. Counts and times are unavailable; refresh to retry.`;
  });
  const jobs = readJSON(`/api/jobs?limit=100${state ? `&state=${encodeURIComponent(state)}` : ""}`).then(({jobs}) => {
    if (current === generation) renderJobs(jobs);
  }).catch((error) => {
    if (current !== generation) return;
    byId("rows").replaceChildren();
    byId("status").textContent = `Deliveries unavailable: ${error.message}. Check the local server and refresh.`;
  });
  await Promise.allSettled([metrics, jobs]);
  if (current === generation) byId("refresh").disabled = false;
}
byId("state").addEventListener("change", refresh);
byId("refresh").addEventListener("click", refresh);
refresh();
