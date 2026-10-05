import { Orb } from "./orb.js";
import { renderMarkdown } from "./markdown.js";

const $ = (id) => document.getElementById(id);
const app = $("app");
const orb = new Orb($("orb"));
const token = new URLSearchParams(location.search).get("token") || "";

const STATE_TEXT = {
  idle: "Ready",
  listening: "Listening",
  transcribing: "Got it",
  thinking: "Thinking",
  tool_execution: "Working",
  speaking: "Speaking",
  interrupted: "Yeah?",
  error: "Something went wrong",
  paused: "Microphone off",
};

// Look-ups that change nothing don't belong in "Recent actions".
const QUIET_TOOLS = new Set(["system.time", "system.date", "calc.evaluate", "memory.recall", "claude.status",
  "media.now_playing", "weather.current", "project.list", "web.lookup", "clipboard.read"]);

const ui = {
  state: "idle",
  engaged: false,
  currentTool: null,
  turns: new Map(), // turn_id -> {el, text}
  pendingAssistant: null,
  events: [],
  metrics: [],
  sessions: new Map(),
  components: {},
  providers: {},
  settings: {},
  memories: [],
  devOpen: false,
  ready: false,
  lastRoute: {},
};

// ------------------------------------------------------------------ connection

let socket = null;
let retry = 500;
let nextId = 1;

function connect() {
  socket = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
  socket.addEventListener("open", () => {
    retry = 500;
  });
  socket.addEventListener("message", (message) => {
    const payload = JSON.parse(message.data);
    if (payload.type === "snapshot") applySnapshot(payload.data);
    else if (payload.type === "events") payload.events.forEach(handleEvent);
    else if (payload.type === "reply" && payload.data && payload.data.components) applyRefresh(payload.data);
  });
  socket.addEventListener("close", () => {
    setStateLine("Reconnecting to Sugar…");
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 8000);
  });
}

function send(cmd, data = {}) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ cmd, id: nextId++, ...data }));
  }
}

// ------------------------------------------------------------------ snapshot

function applySnapshot(data) {
  ui.components = data.components || {};
  ui.providers = data.providers || {};
  ui.settings = data.settings || {};
  ui.memories = data.memories || [];
  renderComponents();
  renderSettings();
  renderMemories();
  setEngaged(data.engaged);
  setMic(data.mic_paused);
  setState(data.state);
  if (data.project) setProject(data.project.name, null);
  (data.sessions || []).forEach((s) => ui.sessions.set(s.project_path, s));
  renderSession();
  if (data.pending_permission) showPermission(data.pending_permission);
  const captions = $("captions");
  captions.querySelectorAll(".turn").forEach((el) => el.remove());
  for (const turn of data.history || []) {
    const el = addTurn(turn.role === "user" ? "user" : "assistant", turn.content);
    if (turn.role === "assistant" && turn.heard != null) el.classList.add("interrupted"); // heard is kept only when cut off
  }
  markLatest();
  if (data.metrics) renderSummary(data.metrics);
  if (Object.values(ui.components).length && !Object.values(ui.components).some((c) => c.status === "loading")) {
    finishBoot();
  }
}

// A refresh (dev panel) updates the side data without touching the live conversation.
function applyRefresh(data) {
  ui.components = data.components || ui.components;
  ui.providers = data.providers || ui.providers;
  ui.memories = data.memories || [];
  renderComponents();
  renderMemories();
  if (data.metrics) renderSummary(data.metrics);
}

// ------------------------------------------------------------------ events

function handleEvent(event) {
  const { type, data } = event;
  logEvent(event);
  switch (type) {
    case "state.changed":
      setState(data.state);
      break;
    case "audio.level":
      orb.setLevels(data.mic || 0, data.out || 0);
      break;
    case "stt.partial":
      $("hearing").textContent = data.text;
      break;
    case "stt.final":
    case "vad.discarded":
    case "stt.ignored":
      $("hearing").textContent = "";
      break;
    case "user.message":
      $("hearing").textContent = "";
      addTurn("user", data.text);
      markLatest();
      break;
    case "turn.started":
      ui.pendingAssistant = null;
      break;
    case "assistant.delta":
      appendAssistant(data.turn_id, data.text);
      break;
    case "assistant.message":
      finishAssistant(data);
      break;
    case "announcement":
      addTurn("assistant", data.text);
      markLatest();
      break;
    case "tool.start":
      ui.currentTool = data.action;
      if (!QUIET_TOOLS.has(data.name)) addActivity(data.name, data.action, "running");
      if (ui.state === "tool_execution") setStateLine(capitalize(data.action));
      break;
    case "tool.complete":
      if (!QUIET_TOOLS.has(data.name)) updateActivity(data.name, data.summary, data.ok ? "done" : "failed", data.ms);
      ui.currentTool = null;
      break;
    case "tool.denied":
      updateActivity(data.name, `Not allowed: ${data.action}`, "failed");
      break;
    case "permission.request":
      showPermission(data);
      break;
    case "permission.resolved":
    case "permission.expired":
      $("permission").hidden = true;
      break;
    case "coding.session":
      ui.sessions.set(data.project_path, data);
      renderSession();
      break;
    case "coding.progress":
      codingProgress(data);
      break;
    case "project.active":
      setProject(data.name, null);
      break;
    case "system.status":
      ui.components[data.component] = { status: data.status, detail: data.detail };
      renderComponents();
      break;
    case "system.ready":
      finishBoot();
      break;
    case "providers.health":
      ui.providers = data.providers;
      renderComponents();
      break;
    case "metrics.turn":
      addMetrics(data);
      break;
    case "route.selected":
      ui.lastRoute = { route: data.route, reason: data.reason, intent: data.intent };
      break;
    case "llm.start":
      ui.lastRoute.provider = data.provider;
      ui.lastRoute.model = data.model;
      break;
    case "conversation.engaged":
      setEngaged(data.engaged);
      break;
    case "audio.mic":
      setMic(data.status === "paused");
      if (data.status === "error") toast(`Microphone problem: ${data.error}`);
      break;
    case "barge_in":
      orb.setState("interrupted");
      break;
    case "error":
      toast(data.message || "Something went wrong.");
      break;
    case "memory.changed":
      send("snapshot");
      break;
    case "settings.changed":
      break;
    default:
      break;
  }
}

// ------------------------------------------------------------------ state

function setState(state) {
  if (!state) return;
  ui.state = state;
  app.dataset.state = state;
  orb.setState(state);
  let text = STATE_TEXT[state] || "";
  if (state === "tool_execution" && ui.currentTool) text = capitalize(ui.currentTool);
  if (state === "idle") text = ui.engaged ? "Listening for you" : "Say “Sugar” to start";
  setStateLine(text);
  $("stopBtn").hidden = !["thinking", "tool_execution", "speaking"].includes(state);
}

function setStateLine(text) {
  $("stateLine").textContent = text;
}

function setEngaged(engaged) {
  ui.engaged = !!engaged;
  $("engagement").textContent = ui.engaged ? "In conversation" : "Say “Sugar” to start";
  if (ui.state === "idle") setState("idle");
}

function setMic(paused) {
  $("micToggle").setAttribute("aria-pressed", paused ? "true" : "false");
  $("micToggle").title = paused ? "Turn the microphone on (Ctrl+M)" : "Turn the microphone off (Ctrl+M)";
  $("micLabel").textContent = paused ? "Microphone off" : "Microphone on";
}

function setProject(name) {
  if (!name) return;
  $("projectChip").hidden = false;
  $("projectName").textContent = name;
}

// ------------------------------------------------------------------ captions

function addTurn(role, text) {
  $("captionsEmpty").hidden = true;
  const el = document.createElement("article");
  el.className = `turn ${role}`;
  el.innerHTML = `<p class="who">${role === "user" ? "You" : "Sugar"}</p><div class="said"></div>`;
  const said = el.querySelector(".said");
  if (role === "user") said.textContent = text;
  else said.innerHTML = renderMarkdown(text || "");
  $("captions").appendChild(el);
  scrollCaptions();
  return el;
}

function appendAssistant(turnId, text) {
  let entry = ui.turns.get(turnId);
  if (!entry) {
    const el = addTurn("assistant", "");
    entry = { el, text: "", frame: 0 };
    ui.turns.set(turnId, entry);
    markLatest();
  }
  entry.text += text;
  if (!entry.frame) {
    entry.frame = requestAnimationFrame(() => {
      entry.frame = 0;
      entry.el.querySelector(".said").innerHTML = renderMarkdown(entry.text);
      scrollCaptions();
    });
  }
}

function finishAssistant(data) {
  if (data.turn_id == null) {
    if (data.text) {
      addTurn("assistant", data.text);
      markLatest();
    }
    return;
  }
  const entry = ui.turns.get(data.turn_id);
  if (!entry) {
    if (data.text) {
      const el = addTurn("assistant", data.text);
      if (data.interrupted) el.classList.add("interrupted");
    }
  } else {
    entry.el.querySelector(".said").innerHTML = renderMarkdown(data.text || entry.text);
    if (data.interrupted) entry.el.classList.add("interrupted");
    ui.turns.delete(data.turn_id);
  }
  markLatest();
  scrollCaptions();
}

function markLatest() {
  const turns = [...document.querySelectorAll(".turn")];
  turns.forEach((t) => t.classList.remove("latest", "latest-pair"));
  const last = turns[turns.length - 1];
  if (!last) return;
  last.classList.add("latest");
  const previous = turns[turns.length - 2];
  if (previous && last.classList.contains("assistant") && previous.classList.contains("user")) {
    previous.classList.add("latest-pair");
  }
}

function scrollCaptions() {
  const captions = $("captions");
  captions.scrollTop = captions.scrollHeight;
}

$("captions").addEventListener("click", (e) => {
  const copy = e.target.closest(".copy");
  if (copy) {
    const code = copy.parentElement.querySelector("code").textContent;
    navigator.clipboard.writeText(code).then(() => {
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy"), 1500);
    });
    return;
  }
  const link = e.target.closest("a[href]");
  if (link) {
    e.preventDefault();
    send("open_url", { url: link.href });
  }
});

// ------------------------------------------------------------------ workbench

const activityItems = [];

function addActivity(name, text, status) {
  activityItems.unshift({ name, text, status, at: new Date() });
  activityItems.length = Math.min(activityItems.length, 12);
  renderActivity();
}

function updateActivity(name, text, status, ms) {
  const item = activityItems.find((i) => i.name === name && i.status === "running");
  if (item) {
    item.text = text || item.text;
    item.status = status;
    item.ms = ms;
  } else {
    activityItems.unshift({ name, text, status, at: new Date(), ms });
  }
  renderActivity();
}

function renderActivity() {
  const list = $("activity");
  list.innerHTML = "";
  for (const item of activityItems) {
    const li = document.createElement("li");
    li.className = item.status;
    const what = document.createElement("span");
    what.className = "what";
    what.textContent = capitalize(item.text || item.name);
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = item.at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    li.append(what, when);
    list.appendChild(li);
  }
  $("activityEmpty").hidden = activityItems.length > 0;
}

function codingProgress(data) {
  const session = [...ui.sessions.values()].find((s) => s.project_name === data.project);
  if (session && data.kind === "tool") {
    session.activity = [...(session.activity || []), data.text].slice(-8);
    renderSession();
  }
}

function renderSession() {
  const sessions = [...ui.sessions.values()].sort((a, b) => b.last_activity - a.last_activity);
  const container = $("session");
  const running = sessions.some((s) => s.status === "running");
  orb.setCoding(running);
  const dot = $("projectDot");
  dot.className = "project-dot" + (running ? " working" : sessions.some((s) => s.status === "waiting_permission") ? " waiting" : "");
  if (!sessions.length) return;
  const s = sessions[0];
  const statusText = {
    idle: "Connected",
    running: "Working",
    paused: "Paused",
    completed: "Finished",
    failed: "Failed",
    cancelled: "Stopped",
    waiting_permission: "Needs your OK",
  }[s.status] || s.status;
  container.innerHTML = "";
  const head = document.createElement("div");
  head.className = "session-head";
  head.innerHTML = `<span class="session-project"></span><span class="session-status ${s.status}"></span>`;
  head.querySelector(".session-project").textContent = s.project_name;
  head.querySelector(".session-status").textContent = statusText;
  container.appendChild(head);
  if (s.task) {
    const task = document.createElement("p");
    task.className = "session-task";
    task.textContent = s.task;
    container.appendChild(task);
  }
  if (s.status === "running" && s.activity && s.activity.length) {
    const steps = document.createElement("ul");
    steps.className = "session-steps";
    for (const line of s.activity.slice(-5)) {
      const li = document.createElement("li");
      li.textContent = line;
      steps.appendChild(li);
    }
    container.appendChild(steps);
  } else if (s.last_summary) {
    const summary = document.createElement("p");
    summary.className = "session-summary";
    summary.textContent = s.last_summary;
    container.appendChild(summary);
  }
  const actions = document.createElement("div");
  actions.className = "session-actions";
  const add = (label, action) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn";
    b.textContent = label;
    b.addEventListener("click", () => send("coding", { action }));
    actions.appendChild(b);
  };
  if (s.status === "running") {
    add("Pause", "pause");
    add("Stop", "stop");
  } else if (["paused", "failed", "cancelled"].includes(s.status)) {
    add("Resume", "resume");
  }
  if (actions.children.length) container.appendChild(actions);
}

function showPermission(request) {
  $("permission").hidden = false;
  $("permission").dataset.id = request.id;
  $("permissionText").textContent = `Sugar wants to ${request.action}.`;
}

$("permissionAllow").addEventListener("click", () => send("permission", { id: $("permission").dataset.id, approved: true }));
$("permissionDeny").addEventListener("click", () => send("permission", { id: $("permission").dataset.id, approved: false }));

// ------------------------------------------------------------------ boot & health

const COMPONENT_NAMES = {
  ui: "Interface",
  speaker: "Speaker",
  microphone: "Microphone",
  stt: "Speech recognition",
  tts: "Voice",
  projects: "Projects",
  apps: "Apps",
  claude_code: "Claude Code",
  "llm:freellm": "FreeLLMAPI",
  "llm:ollama": "Ollama (offline)",
  "llm:claude": "Claude",
};

function renderComponents() {
  const boot = $("bootList");
  boot.innerHTML = "";
  const health = $("health");
  health.innerHTML = "";
  for (const [name, info] of Object.entries(ui.components)) {
    const label = COMPONENT_NAMES[name] || name;
    const li = document.createElement("li");
    li.className = info.status;
    li.textContent = info.status === "loading" ? `${label}…` : label;
    boot.appendChild(li);

    const row = document.createElement("li");
    row.className = info.status;
    row.innerHTML = `<span class="dot"></span><span class="name"></span><span class="detail"></span>`;
    row.querySelector(".name").textContent = label;
    row.querySelector(".detail").textContent = `${info.status}${info.detail ? " — " + info.detail : ""}`;
    health.appendChild(row);
  }
  const providers = $("providers");
  providers.innerHTML = "";
  for (const [name, info] of Object.entries(ui.providers || {})) {
    const row = document.createElement("li");
    const ok = info.available !== undefined ? info.available : info.ok;
    row.className = ok ? "ok" : "down";
    row.innerHTML = `<span class="dot"></span><span class="name"></span><span class="detail"></span>`;
    row.querySelector(".name").textContent = `Model: ${name}`;
    row.querySelector(".detail").textContent = info.last_error || info.health || info.detail || (ok ? "available" : "unavailable");
    providers.appendChild(row);
  }
}

function finishBoot() {
  if (ui.ready) return;
  ui.ready = true;
  $("boot").classList.add("done");
  const problems = Object.entries(ui.components).filter(([, c]) => c.status === "error");
  if (problems.length) toast(`Some parts didn't start: ${problems.map(([n]) => COMPONENT_NAMES[n] || n).join(", ")}. Details in the developer panel.`);
}

// ------------------------------------------------------------------ developer panel

function logEvent(event) {
  if (event.type === "audio.level" || event.type === "assistant.delta") return;
  ui.events.unshift(event);
  ui.events.length = Math.min(ui.events.length, 400);
  if (ui.devOpen) renderEvents();
}

function renderEvents() {
  const filter = $("eventFilter").value.trim().toLowerCase();
  const list = $("events");
  list.innerHTML = "";
  for (const event of ui.events.slice(0, 200)) {
    if (filter && !event.type.includes(filter)) continue;
    const li = document.createElement("li");
    const time = new Date(event.ts * 1000).toLocaleTimeString([], { hour12: false });
    li.innerHTML = `<span class="time"></span><span class="type"></span><span class="data"></span>`;
    li.querySelector(".time").textContent = time;
    li.querySelector(".type").textContent = event.type;
    li.querySelector(".data").textContent = JSON.stringify(event.data).slice(0, 300);
    list.appendChild(li);
  }
}

function addMetrics(record) {
  ui.metrics.unshift({ ...record, route: { ...ui.lastRoute } });
  ui.metrics.length = Math.min(ui.metrics.length, 15);
  renderMetrics();
}

function cell(ms, slow, verySlow) {
  if (ms == null) return `<td>—</td>`;
  const cls = ms >= verySlow ? "very-slow" : ms >= slow ? "slow" : "";
  const width = Math.min(80, Math.round(ms / 50));
  return `<td class="${cls}">${ms} ms<span class="bar" style="width:${width}px"></span></td>`;
}

function renderMetrics() {
  const body = $("metricsTable").querySelector("tbody");
  body.innerHTML = "";
  for (const m of ui.metrics) {
    const metrics = m.metrics || {};
    const info = m.info || {};
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${m.turn_id}</td><td></td><td></td>` +
      cell(metrics.endpoint_ms, 600, 1200) +
      cell(metrics.stt_wait_ms, 300, 800) +
      cell(metrics.llm_ttft_ms, 1500, 3000) +
      cell(metrics.tts_first_audio_ms, 300, 800) +
      cell(metrics.voice_to_voice_ms ?? metrics.text_to_voice_ms, 2000, 4000);
    tr.children[1].textContent = info.route || m.route.route || "—";
    tr.children[2].textContent = info.model || m.route.model || "—";
    body.appendChild(tr);
  }
  const last = ui.metrics[0];
  const box = $("lastTurn");
  box.innerHTML = "";
  if (last) {
    const fields = {
      Route: (last.info && last.info.route) || last.route.route,
      Reason: last.route.reason,
      Provider: last.route.provider,
      Model: (last.info && last.info.model) || last.route.model,
      Tools: (last.info && last.info.tools || []).join(", ") || "none",
      Interrupted: last.info && last.info.interrupted ? "yes" : "no",
    };
    for (const [k, v] of Object.entries(fields)) {
      const div = document.createElement("div");
      div.innerHTML = `<span></span><strong></strong>`;
      div.querySelector("span").textContent = k;
      div.querySelector("strong").textContent = v || "—";
      box.appendChild(div);
    }
  }
}

function renderSummary(summary) {
  const box = $("metricsSummary");
  box.innerHTML = "";
  const labels = { voice_to_voice_ms: "Voice to voice", llm_ttft_ms: "First token", stt_wait_ms: "STT wait",
    tts_first_audio_ms: "First audio", endpoint_ms: "End of speech", interrupt_ms: "Interruption" };
  for (const [key, label] of Object.entries(labels)) {
    const s = summary[key];
    if (!s) continue;
    const div = document.createElement("div");
    div.innerHTML = `<span></span> <strong></strong>`;
    div.querySelector("span").textContent = `${label} (p50 / p90, n=${s.count})`;
    div.querySelector("strong").textContent = `${Math.round(s.p50)} / ${Math.round(s.p90)} ms`;
    box.appendChild(div);
  }
}

function renderMemories() {
  const list = $("memories");
  list.innerHTML = "";
  if (!ui.memories.length) {
    list.innerHTML = `<li><span class="scope">Nothing remembered yet. Say “remember that…”.</span></li>`;
    return;
  }
  for (const m of ui.memories) {
    const li = document.createElement("li");
    li.innerHTML = `<div><div class="content"></div><div class="scope"></div></div><button type="button" class="btn">Forget</button>`;
    li.querySelector(".content").textContent = m.content;
    li.querySelector(".scope").textContent = `${m.kind}, ${m.scope.startsWith("project:") ? "project" : m.scope}`;
    li.querySelector("button").addEventListener("click", () => {
      send("memory.delete", { id: m.id });
      li.remove();
    });
    list.appendChild(li);
  }
}

function openDev(open) {
  ui.devOpen = open;
  $("devPanel").hidden = !open;
  $("devToggle").setAttribute("aria-expanded", String(open));
  if (open) {
    renderEvents();
    renderMetrics();
    send("snapshot");
  }
}

$("devToggle").addEventListener("click", () => openDev(!ui.devOpen));
$("devClose").addEventListener("click", () => openDev(false));
$("eventFilter").addEventListener("input", renderEvents);
document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.dataset.panel !== tab.dataset.tab));
  })
);

// ------------------------------------------------------------------ settings

function renderSettings() {
  const s = ui.settings;
  if (!s.tts) return;
  $("speed").value = s.tts.speed;
  $("speedValue").textContent = `${Number(s.tts.speed).toFixed(2)}×`;
  $("sensitivity").value = s.vad.threshold;
  $("sensitivityValue").textContent = Number(s.vad.threshold).toFixed(2);
  $("patience").value = s.vad.endpoint_default_ms;
  $("patienceValue").textContent = `${s.vad.endpoint_default_ms} ms`;
  document.querySelectorAll('input[name="engagement"]').forEach((r) => (r.checked = r.value === s.conversation.engagement));
}

function bindSetting(id, key, format) {
  const input = $(id);
  input.addEventListener("input", () => ($(`${id}Value`).textContent = format(input.value)));
  input.addEventListener("change", () => send("setting", { key, value: Number(input.value) }));
}
bindSetting("speed", "tts.speed", (v) => `${Number(v).toFixed(2)}×`);
bindSetting("sensitivity", "vad.threshold", (v) => Number(v).toFixed(2));
bindSetting("patience", "vad.endpoint_default_ms", (v) => `${v} ms`);
document.querySelectorAll('input[name="engagement"]').forEach((r) =>
  r.addEventListener("change", () => send("setting", { key: "conversation.engagement", value: r.value }))
);
$("newConversation").addEventListener("click", () => {
  send("new_conversation");
  document.querySelectorAll(".turn").forEach((t) => t.remove());
  $("captionsEmpty").hidden = false;
});
$("settingsToggle").addEventListener("click", () => {
  const open = $("settingsPanel").hidden;
  $("settingsPanel").hidden = !open;
  $("settingsToggle").setAttribute("aria-expanded", String(open));
});

// ------------------------------------------------------------------ input

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  send("text", { text });
  $("input").value = "";
});
$("stopBtn").addEventListener("click", () => send("stop"));
$("micToggle").addEventListener("click", () => {
  const paused = $("micToggle").getAttribute("aria-pressed") === "true";
  send("mic", { on: paused });
  setMic(!paused);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "`" && document.activeElement !== $("input")) {
    e.preventDefault();
    openDev(!ui.devOpen);
  } else if (e.key === "Escape") {
    if (ui.devOpen) openDev(false);
    else if (!$("settingsPanel").hidden) $("settingsToggle").click();
    else send("stop");
  } else if (e.key.toLowerCase() === "m" && e.ctrlKey) {
    e.preventDefault();
    $("micToggle").click();
  }
});

// ------------------------------------------------------------------ helpers

let toastTimer = 0;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 6000);
}

function capitalize(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

connect();
