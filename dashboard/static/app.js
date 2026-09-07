// Pi Bot Dashboard — frontend

const state = {
  page: "overview",
  bots: [],
  logBot: null,
  prompts: [],
  promptBot: null,
  promptOriginals: {},
  offline: false,
};

// ─── Fetch helpers ────────────────────────────────────────────────────────

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  setOnline(true);
  if (!res.ok) {
    let detail = "Request failed";
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

function setOnline(ok) {
  if (state.offline === !ok) return;
  state.offline = !ok;
  document.getElementById("offline").classList.toggle("on", !ok);
}

function toast(msg, bad = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.toggle("bad", bad);
  t.classList.add("on");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("on"), 4200);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ─── Navigation ───────────────────────────────────────────────────────────

document.getElementById("nav").addEventListener("click", e => {
  const btn = e.target.closest("button[data-page]");
  if (btn) go(btn.dataset.page);
});

function go(page) {
  state.page = page;
  document.querySelectorAll("nav button").forEach(b =>
    b.classList.toggle("on", b.dataset.page === page));
  document.querySelectorAll(".page").forEach(p =>
    p.classList.toggle("on", p.id === "page-" + page));

  if (page === "channels")    loadChannels();
  if (page === "prompts")     loadPrompts();
  if (page === "logs")        loadLog();
  if (page === "schedule")    loadSchedule();
  if (page === "diagnostics") loadSystem();
  if (page === "settings")    loadRecipients();
}

// ─── Bots ─────────────────────────────────────────────────────────────────

async function loadBots() {
  try {
    state.bots = await api("/api/bots");
  } catch (e) {
    setOnline(false);
    return;
  }
  renderPips();
  renderOverview();
  renderBotCards();
  renderAlert();
  fillLogPicker();
}

function pipClass(b) {
  if (b.busy) return "busy";
  return b.healthy ? "up" : "down";
}

function renderPips() {
  document.getElementById("pips").innerHTML = state.bots.map(b => `
    <button class="pip ${pipClass(b)}" data-bot="${b.id}" title="${esc(b.name)} — ${esc(b.label)}">
      <i class="led"></i><span>${esc(b.name.replace(" Bot", ""))}</span>
    </button>`).join("");

  document.querySelectorAll(".pip").forEach(p =>
    p.addEventListener("click", () => go("bots")));
}

function renderAlert() {
  const down = state.bots.filter(b => !b.healthy && !b.busy);
  const box = document.getElementById("globalAlert");
  if (down.length) {
    box.textContent = down.length === 1
      ? `${down[0].name} is ${down[0].label.toLowerCase()}.`
      : `${down.length} bots need attention: ${down.map(b => b.name).join(", ")}.`;
    box.className = "alert on";
  } else {
    box.className = "alert";
  }
}

function stateChip(b) {
  const cls = b.busy ? "busy" : (b.healthy ? "up" : "down");
  return `<span class="state ${cls}">${esc(b.label)}</span>`;
}

function renderOverview() {
  document.getElementById("overviewCards").innerHTML = state.bots.map(b => `
    <div class="card">
      <div class="card-head">
        <h3>${esc(b.name)}</h3>
        ${stateChip(b)}
      </div>
      <p class="blurb">${esc(b.blurb)}</p>
      <div class="meta">
        ${b.last_activity ? "Last log entry " + esc(b.last_activity) : "No log activity yet"}
      </div>
      <div class="row">
        <button class="btn sm" data-act="restart" data-bot="${b.id}">Restart</button>
        <button class="btn sm" data-log="${b.id}">View log</button>
      </div>
    </div>`).join("");
}

function renderBotCards() {
  document.getElementById("botCards").innerHTML = state.bots.map(b => {
    const runs = b.runs.map(r =>
      `<button class="btn sm" data-run="${b.id}" data-mode="${esc(r.mode)}">${esc(r.label)}</button>`
    ).join("");

    const svcLabel = b.kind === "scheduled" ? "timer" : "service";

    return `
      <div class="card">
        <div class="card-head">
          <h3>${esc(b.name)}</h3>
          ${stateChip(b)}
        </div>
        <p class="blurb">${esc(b.blurb)}</p>
        <div class="meta">
          ${esc(b.service)}.${svcLabel} · ${esc(b.enabled || "unknown")}<br>
          ${b.last_activity ? "Last entry " + esc(b.last_activity) : "No log activity yet"}
        </div>
        <div class="row" style="margin-bottom:7px">
          <button class="btn sm" data-act="start"   data-bot="${b.id}">Start</button>
          <button class="btn sm" data-act="restart" data-bot="${b.id}">Restart</button>
          <button class="btn sm danger" data-act="stop" data-bot="${b.id}">Stop</button>
        </div>
        <div class="row">
          ${runs}
          <button class="btn sm" data-log="${b.id}">View log</button>
        </div>
      </div>`;
  }).join("");
}

// Delegated clicks for every bot action
document.addEventListener("click", async e => {
  const svc = e.target.closest("[data-act]");
  if (svc) {
    const { act, bot } = svc.dataset;
    svc.disabled = true;
    try {
      const r = await api(`/api/bots/${bot}/service`, {
        method: "POST",
        body: JSON.stringify({ action: act }),
      });
      toast(r.message);
      setTimeout(loadBots, 900);
    } catch (err) { toast(err.message, true); }
    finally { svc.disabled = false; }
    return;
  }

  const run = e.target.closest("[data-run]");
  if (run) {
    const { run: bot, mode } = run.dataset;
    run.disabled = true;
    try {
      const r = await api(`/api/bots/${bot}/run`, {
        method: "POST",
        body: JSON.stringify({ mode }),
      });
      toast(r.message);
      setTimeout(loadBots, 1200);
    } catch (err) { toast(err.message, true); }
    finally { run.disabled = false; }
    return;
  }

  const viewLog = e.target.closest("[data-log]");
  if (viewLog) {
    state.logBot = viewLog.dataset.log;
    go("logs");
    document.getElementById("logPick").value = state.logBot;
    loadLog();
  }
});

// ─── Logs ─────────────────────────────────────────────────────────────────

function fillLogPicker() {
  const sel = document.getElementById("logPick");
  if (sel.options.length === state.bots.length && sel.value) return;
  sel.innerHTML = state.bots.map(b =>
    `<option value="${b.id}">${esc(b.name)}</option>`).join("");
  if (state.logBot) sel.value = state.logBot;
}

async function loadLog() {
  const sel   = document.getElementById("logPick");
  const bot   = sel.value || (state.bots[0] && state.bots[0].id);
  if (!bot) return;
  state.logBot = bot;

  const lines = document.getElementById("logLines").value;
  const body  = document.getElementById("logBody");

  try {
    const r = await api(`/api/bots/${bot}/log?lines=${lines}`);
    renderLog(r.content);
  } catch (e) {
    body.innerHTML = `<span class="empty">Could not read the log: ${esc(e.message)}</span>`;
  }
}

function renderLog(content) {
  const body = document.getElementById("logBody");
  const find = document.getElementById("logFind").value.trim().toLowerCase();

  if (!content) {
    body.innerHTML = '<span class="empty">This log is empty. It fills in once the bot runs.</span>';
    return;
  }

  let lines = content.split("\n");
  if (find) lines = lines.filter(l => l.toLowerCase().includes(find));

  if (!lines.length) {
    body.innerHTML = `<span class="empty">No lines match “${esc(find)}”.</span>`;
    return;
  }

  body.innerHTML = lines.map(l => {
    const safe = esc(l);
    if (!find) return safe;
    const re = new RegExp(`(${find.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig");
    return safe.replace(re, '<span class="hit">$1</span>');
  }).join("\n");

  body.scrollTop = body.scrollHeight;
}

document.getElementById("logPick").addEventListener("change", loadLog);
document.getElementById("logLines").addEventListener("change", loadLog);
document.getElementById("logRefresh").addEventListener("click", loadLog);
document.getElementById("logFind").addEventListener("input", loadLog);

document.getElementById("logClear").addEventListener("click", async () => {
  if (!state.logBot) return;
  const bot = state.bots.find(b => b.id === state.logBot);
  if (!confirm(`Clear the ${bot ? bot.name : ""} log? The file is emptied, not deleted.`)) return;
  try {
    const r = await api(`/api/bots/${state.logBot}/log/clear`, { method: "POST" });
    toast(r.message);
    loadLog();
  } catch (e) { toast(e.message, true); }
});

// ─── Prompts ──────────────────────────────────────────────────────────────

function promptBox(id)  { return document.querySelector(`[data-box="${id}"]`); }
function promptDirty(id) {
  const box = promptBox(id);
  return box && box.value !== (state.promptOriginals[id] ?? "");
}
function anyPromptDirty() { return state.prompts.some(p => promptDirty(p.id)); }

function promptNote(html) {
  document.getElementById("promptList").innerHTML =
    `<div class="card"><p class="empty-note">${html}</p></div>`;
}

async function loadPrompts() {
  const botSel = document.getElementById("promptBot");

  // Always fetch fresh rather than trusting state.bots — this page is often the
  // first thing opened, and a stale/empty list used to leave the picker blank
  // with nothing on screen to say why.
  let bots;
  try {
    bots = await api("/api/bots");
    state.bots = bots;
  } catch (e) {
    botSel.innerHTML = "";
    promptNote(`Couldn't reach the dashboard API: ${esc(e.message)}`);
    return;
  }

  const editable = bots.filter(b => b.has_prompts);
  if (!editable.length) {
    botSel.innerHTML = "";
    promptNote(`The API returned ${bots.length} bot(s), none flagged as having prompts. ` +
               `If this is unexpected, dashboard/config.py may be missing its "prompts" entries.`);
    return;
  }

  botSel.innerHTML = editable.map(b =>
    `<option value="${esc(b.id)}">${esc(b.name)}</option>`).join("");
  if (state.promptBot && editable.some(b => b.id === state.promptBot)) {
    botSel.value = state.promptBot;
  }
  state.promptBot = botSel.value || editable[0].id;

  try {
    state.prompts = await api(`/api/bots/${state.promptBot}/prompts`);
  } catch (e) {
    promptNote(`Couldn't load prompts for this bot: ${esc(e.message)}`);
    return;
  }

  renderPrompts();
}

function renderPrompts() {
  const wrap = document.getElementById("promptList");

  if (!state.prompts.length) {
    wrap.innerHTML = '<div class="card"><p class="empty-note">This bot has no editable prompt.</p></div>';
    return;
  }

  wrap.innerHTML = state.prompts.map(p => `
    <div class="card section-gap">
      <div class="card-head">
        <h3>${esc(p.label)}</h3>
        <span class="prompt-state" data-state="${esc(p.id)}"></span>
      </div>
      <p class="blurb">${esc(p.desc)}</p>
      <textarea class="prompt-box" data-box="${esc(p.id)}" spellcheck="false"></textarea>
      <div class="err" data-err="${esc(p.id)}"></div>
      <div class="row section-gap">
        <button class="btn primary" data-save="${esc(p.id)}">Save</button>
        <button class="btn" data-undo="${esc(p.id)}">Undo my edits</button>
        <button class="btn danger" data-reset="${esc(p.id)}" ${p.modified ? "" : "disabled"}>Reset to original</button>
      </div>
    </div>`).join("");

  // Assign through .value so the prompt text is never mangled by HTML parsing.
  state.promptOriginals = {};
  state.prompts.forEach(p => {
    state.promptOriginals[p.id] = p.text;
    promptBox(p.id).value = p.text;
    markPromptState(p.id);
  });

  wrap.querySelectorAll("[data-box]").forEach(box =>
    box.addEventListener("input", () => markPromptState(box.dataset.box)));

  wrap.querySelectorAll("[data-save]").forEach(btn =>
    btn.addEventListener("click", () => savePrompt(btn.dataset.save)));

  wrap.querySelectorAll("[data-undo]").forEach(btn =>
    btn.addEventListener("click", () => {
      const id = btn.dataset.undo;
      promptBox(id).value = state.promptOriginals[id] ?? "";
      wrap.querySelector(`[data-err="${id}"]`).classList.remove("on");
      markPromptState(id);
    }));

  wrap.querySelectorAll("[data-reset]").forEach(btn =>
    btn.addEventListener("click", () => resetPrompt(btn.dataset.reset)));
}

function markPromptState(id) {
  const el = document.querySelector(`[data-state="${id}"]`);
  if (!el) return;
  const p = state.prompts.find(x => x.id === id);
  if (promptDirty(id)) {
    el.textContent = "Unsaved changes";
    el.className = "prompt-state dirty";
  } else if (p && p.modified) {
    el.textContent = "Customised";
    el.className = "prompt-state mod";
  } else {
    el.textContent = "Original";
    el.className = "prompt-state";
  }
}

async function savePrompt(id) {
  const err = document.querySelector(`[data-err="${id}"]`);
  try {
    const r = await api(`/api/bots/${state.promptBot}/prompts/${id}`, {
      method: "PUT",
      body: JSON.stringify({ text: promptBox(id).value }),
    });
    err.classList.remove("on");
    toast(r.message);
    await loadPrompts();
  } catch (e) {
    err.textContent = e.message;
    err.classList.add("on");
  }
}

async function resetPrompt(id) {
  const p = state.prompts.find(x => x.id === id);
  if (!confirm(`Reset "${p ? p.label : ""}" to the original?\n\nYour current version is kept as a .bak file next to it.`)) return;
  try {
    const r = await api(`/api/bots/${state.promptBot}/prompts/${id}/reset`, { method: "POST" });
    toast(r.message);
    await loadPrompts();
  } catch (e) { toast(e.message, true); }
}

document.getElementById("promptBot").addEventListener("change", e => {
  if (anyPromptDirty() &&
      !confirm("You have unsaved changes to this bot's prompt. Discard them?")) {
    e.target.value = state.promptBot;
    return;
  }
  state.promptBot = e.target.value;
  loadPrompts();
});

// ─── Channels ─────────────────────────────────────────────────────────────

async function loadChannels() {
  let data;
  try { data = await api("/api/channels"); }
  catch (e) { toast(e.message, true); return; }

  const rows = data.channels || [];
  const tbody = document.querySelector("#channelTable tbody");
  document.getElementById("channelEmpty").style.display = rows.length ? "none" : "block";
  document.getElementById("channelTable").style.display = rows.length ? "table" : "none";

  tbody.innerHTML = rows.map(c => `
    <tr>
      <td>
        <div>${esc(c.name)}</div>
        <div class="dim mono">${esc(c.id)}</div>
      </td>
      <td>${esc(c.niche || "")}</td>
      <td style="text-align:right">
        <button class="btn sm danger" data-delch="${esc(c.id)}">Remove</button>
      </td>
    </tr>`).join("");

  tbody.querySelectorAll("[data-delch]").forEach(btn =>
    btn.addEventListener("click", async () => {
      if (!confirm("Remove this channel? The bot stops watching it.")) return;
      try {
        const r = await api(`/api/channels/${encodeURIComponent(btn.dataset.delch)}`,
                            { method: "DELETE" });
        toast(r.message);
        loadChannels();
      } catch (e) { toast(e.message, true); }
    }));
}

document.getElementById("addChannel").addEventListener("click", async () => {
  const id    = document.getElementById("chId").value.trim();
  const name  = document.getElementById("chName").value.trim();
  const niche = document.getElementById("chNiche").value.trim();
  const err   = document.getElementById("chErr");

  if (!id || !name) {
    err.textContent = "Channel ID and name are both required.";
    err.classList.add("on");
    return;
  }
  if (!id.startsWith("UC")) {
    err.textContent = "That doesn't look like a channel ID — they start with UC.";
    err.classList.add("on");
    return;
  }
  err.classList.remove("on");

  try {
    const r = await api("/api/channels", {
      method: "POST",
      body: JSON.stringify({ id, name, niche }),
    });
    toast(r.message);
    ["chId", "chName", "chNiche"].forEach(i => document.getElementById(i).value = "");
    loadChannels();
  } catch (e) {
    err.textContent = e.message;
    err.classList.add("on");
  }
});

["chId", "chName"].forEach(i =>
  document.getElementById(i).addEventListener("input", () =>
    document.getElementById("chErr").classList.remove("on")));

// ─── Schedule ─────────────────────────────────────────────────────────────

async function loadSchedule() {
  let rows;
  try { rows = await api("/api/schedule"); }
  catch (e) { toast(e.message, true); return; }

  const body = document.getElementById("scheduleBody");
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="dim">No systemd timers found.</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => `
    <tr>
      <td>${esc(r.name)}<div class="dim mono">${esc(r.unit)}</div></td>
      <td class="mono">${esc(r.next)}</td>
      <td class="mono">${esc(r.left)}</td>
      <td class="mono">${esc(r.last)}</td>
    </tr>`).join("");
}

// ─── System stats ─────────────────────────────────────────────────────────

function barClass(pct, warn = 70, hot = 88) {
  if (pct >= hot) return "hot";
  if (pct >= warn) return "warn";
  return "";
}

function statCard(label, value, sub, pct) {
  const bar = pct === undefined ? "" :
    `<div class="bar"><span class="${barClass(pct)}" style="width:${Math.min(pct,100)}%"></span></div>`;
  return `<div class="card stat">
    <div class="label">${esc(label)}</div>
    <div class="value">${esc(value)}</div>
    <div class="sub">${esc(sub)}</div>
    ${bar}
  </div>`;
}

async function loadSystem() {
  let s;
  try { s = await api("/api/system"); }
  catch (e) { setOnline(false); return; }

  const tempPct = s.cpu_temp ? (s.cpu_temp / 85) * 100 : 0;
  const cards = [
    statCard("CPU", s.cpu_percent + "%", `load ${s.load.join(" / ")}`, s.cpu_percent),
    statCard("Temperature",
             s.cpu_temp ? s.cpu_temp + "°C" : "n/a",
             s.cpu_temp >= 70 ? "running hot" : "normal", tempPct),
    statCard("Memory", s.mem_percent + "%", `${s.mem_used} of ${s.mem_total} GB`, s.mem_percent),
    statCard("Disk", s.disk_percent + "%", `${s.disk_used} of ${s.disk_total} GB`, s.disk_percent),
    statCard("Uptime", s.uptime, "since " + s.booted),
  ].join("");

  document.getElementById("diagStats").innerHTML = cards;
  document.getElementById("overviewStats").innerHTML = cards;
  document.getElementById("clock").textContent = s.server_time;

  if (s.reboot_required) {
    const box = document.getElementById("globalAlert");
    if (!box.classList.contains("on")) {
      box.textContent = "A system update needs a reboot to finish applying.";
      box.className = "alert info on";
    }
  }
}

// ─── Recipients ───────────────────────────────────────────────────────────

async function loadRecipients() {
  let rows;
  try { rows = await api("/api/recipients"); }
  catch (e) { toast(e.message, true); return; }

  document.getElementById("recipientCards").innerHTML = rows.map(r => `
    <div class="card">
      <h3>${esc(r.name)}</h3>
      <p class="blurb">Reports from this bot go to these addresses.</p>
      <div class="field">
        <label for="rcp-${r.bot_id}">Recipients</label>
        <input type="text" id="rcp-${r.bot_id}" value="${esc(r.recipients)}"
               placeholder="you@gmail.com">
        <div class="hint">Separate multiple addresses with commas.</div>
      </div>
      <div class="err" id="rcperr-${r.bot_id}"></div>
      <button class="btn primary" data-rcp="${r.bot_id}">Save</button>
    </div>`).join("");

  document.querySelectorAll("[data-rcp]").forEach(btn =>
    btn.addEventListener("click", async () => {
      const id    = btn.dataset.rcp;
      const value = document.getElementById("rcp-" + id).value.trim();
      const err   = document.getElementById("rcperr-" + id);

      if (!value) {
        err.textContent = "Enter at least one address.";
        err.classList.add("on");
        return;
      }
      if (!value.split(",").every(a => a.includes("@") && a.trim().length > 3)) {
        err.textContent = "One of those doesn't look like an email address.";
        err.classList.add("on");
        return;
      }
      err.classList.remove("on");

      try {
        const r = await api("/api/recipients", {
          method: "POST",
          body: JSON.stringify({ bot_id: id, recipients: value }),
        });
        toast(r.message);
      } catch (e) { toast(e.message, true); }
    }));
}

// ─── System actions ───────────────────────────────────────────────────────

document.querySelectorAll("[data-sys]").forEach(btn =>
  btn.addEventListener("click", async () => {
    const action = btn.dataset.sys;
    const ask = btn.dataset.confirm;
    if (ask && !confirm(ask)) return;
    if (action === "clear-all-logs" &&
        !confirm("Clear every bot log? The files are emptied, not deleted.")) return;
    if (action === "stop-all" &&
        !confirm("Stop all bots? Nothing runs until you start them again.")) return;

    btn.disabled = true;
    try {
      const r = await api("/api/system/action", {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      toast(r.message);
      setTimeout(loadBots, 1500);
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  }));

document.getElementById("showUpdaterLog").addEventListener("click", async () => {
  const box = document.getElementById("updaterLog");
  box.style.display = "block";
  try {
    const r = await api("/api/updater/log?lines=150");
    box.textContent = r.content || "The updater hasn't run yet.";
  } catch (e) {
    box.textContent = "Could not read the updater log: " + e.message;
  }
});

// ─── Polling ──────────────────────────────────────────────────────────────

loadBots();
loadSystem();

setInterval(loadBots, 10000);
setInterval(loadSystem, 15000);
setInterval(() => {
  if (state.page === "logs" && document.getElementById("logAuto").checked) loadLog();
}, 8000);
