"use strict";
const byId = (id) => document.getElementById(id);
let generation = 0;
let detailGeneration = 0;
const labels = {queued: "Queued", leased: "Leased", succeeded: "Succeeded", dead_letter: "Dead letter"};
const date = (seconds) => new Date(seconds * 1000).toLocaleString();

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

async function refresh() {
  const current = ++generation;
  const state = byId("state").value;
  byId("refresh").disabled = true;
  byId("status").textContent = "Loading deliveries…";
  try {
    const [{jobs}, {counts}] = await Promise.all([readJSON(`/api/jobs?limit=100${state ? `&state=${encodeURIComponent(state)}` : ""}`), readJSON("/api/stats")]);
    if (current !== generation) return;
    for (const key of Object.keys(labels)) byId(key).textContent = String(counts[key]);
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
    byId("status").textContent = jobs.length ? `${jobs.length} ${jobs.length === 1 ? "delivery" : "deliveries"} shown · updated ${new Date().toLocaleTimeString()}` : "No deliveries match this state. Send a signed webhook or run the local demo.";
  } catch (error) {
    if (current === generation) byId("status").textContent = `Inbox unavailable: ${error.message}. Check the local server and refresh.`;
  } finally {
    if (current === generation) byId("refresh").disabled = false;
  }
}
byId("state").addEventListener("change", refresh);
byId("refresh").addEventListener("click", refresh);
refresh();
