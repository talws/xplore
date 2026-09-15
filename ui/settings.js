// settings.js — AI settings: provider, key, models, spending limits.
// Keys are sent once, when saved, and are never sent back: the server only
// says whether one is set, where from, and its last four characters.
import { $, esc, api } from "./util.js";

let data = null;

const dialog = () => $("#settings");

function msg(text, kind = "") {
  const m = $("#s-msg");
  m.textContent = text;
  m.className = "s-msg" + (kind ? " " + kind : "");
}

const current = () => data.providers.find(p => p.id === $("#s-provider").value) || data.providers[0];

function fillProvider() {
  const p = current();
  $("#s-fast").value = p.fast;
  $("#s-fast").placeholder = p.defaultFast;
  $("#s-quality").value = p.quality;
  $("#s-quality").placeholder = p.defaultQuality;
  $("#s-key").value = "";
  $("#s-key").placeholder = p.keySet ? `saved (${p.keyHint})` : "paste your API key";
  $("#s-key-state").textContent = p.keySet
    ? (p.keySource === "environment"
        ? `Using the key in the ${p.env} environment variable (${p.keyHint}). A key saved here would take its place.`
        : `A key is saved (${p.keyHint}). Leave the box empty to keep it.`)
    : `No key yet. Paste one here, or set the ${p.env} environment variable.`;
  $("#s-note").textContent = p.note
    ? p.note
    : p.priceKnown ? "" : "Xplore doesn't know this provider's prices, so the limits below assume a high price.";
  $("#s-clear").hidden = p.keySource !== "settings";
  $("#s-test").disabled = !p.available;
}

function render() {
  $("#s-provider").innerHTML = data.providers.map(p =>
    `<option value="${esc(p.id)}"${p.id === data.provider ? " selected" : ""}${p.available ? "" : " disabled"}>`
    + `${esc(p.label)}${p.available ? "" : " (not available yet)"}</option>`).join("");
  fillProvider();
  $("#s-per-request").value = data.budget.per_request_usd;
  $("#s-per-day").value = data.budget.per_day_usd;
  $("#s-spent").textContent = `Spent on AI today (UTC): $${Number(data.spentTodayUsd).toFixed(4)}. `
    + `Settings are kept in ${data.configPath}.`;
}

async function send(path, body) {
  let r;
  try {
    r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify(body || {}) });
  } catch (e) { throw new Error("server unreachable"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `server error ${r.status}`);
  return j;
}

async function save(extra = {}) {
  data = await send("/api/settings", {
    provider: $("#s-provider").value,
    fast: $("#s-fast").value.trim(),
    quality: $("#s-quality").value.trim(),
    key: $("#s-key").value.trim(),
    budget: { per_request_usd: $("#s-per-request").value, per_day_usd: $("#s-per-day").value },
    ...extra,
  });
  render();
  document.dispatchEvent(new CustomEvent("xp:ai-settings", { detail: data }));
}

export async function openSettings() {
  msg("");
  if (!dialog().open) dialog().showModal();
  try {
    data = await api("/api/settings");
    render();
  } catch (e) {
    msg("Couldn't load settings: " + e.message, "bad");
  }
}

/** Whether AI can be used right now, for the Related drawer's default. */
export async function aiReady() {
  try {
    const s = data || (data = await api("/api/settings"));
    const p = s.providers.find(x => x.id === s.provider);
    return !!(p && p.available && p.keySet);
  } catch (e) { return false; }
}

function init() {
  $("#settings-btn").addEventListener("click", openSettings);
  document.addEventListener("xp:open-settings", openSettings);
  $("#s-provider").addEventListener("change", fillProvider);
  $("#s-cancel").addEventListener("click", () => dialog().close());
  $("#s-save").addEventListener("click", async () => {
    msg("Saving…");
    try { await save(); msg("Saved.", "ok"); } catch (e) { msg(e.message, "bad"); }
  });
  $("#s-clear").addEventListener("click", async () => {
    msg("Removing…");
    try { await save({ clearKey: true, key: "" }); msg("Saved key removed.", "ok"); } catch (e) { msg(e.message, "bad"); }
  });
  $("#s-test").addEventListener("click", async () => {
    msg("Saving and testing…");
    try {
      await save();
      const r = await send("/api/settings/test");
      msg(r.ok
        ? `Works. ${r.model} answered, using ${r.input_tokens + r.output_tokens} tokens ($${r.cost_usd.toFixed(5)}).`
        : "The provider answered, but not as expected.", r.ok ? "ok" : "bad");
      data = await api("/api/settings");
      $("#s-spent").textContent = `Spent on AI today (UTC): $${Number(data.spentTodayUsd).toFixed(4)}. `
        + `Settings are kept in ${data.configPath}.`;
    } catch (e) { msg(e.message, "bad"); }
  });
}

init();
