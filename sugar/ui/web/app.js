import { StateTracker } from "./entity-state.js";
import { renderMarkdown } from "./markdown.js";
import { buildWorkbench, NO_PROJECT } from "./workbench-model.js";

const $ = (id) => document.getElementById(id);
const app = $("app");
const token = new URLSearchParams(location.search).get("token") || "";
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const tracker = new StateTracker();
const ui = {
  turns: new Map(), // turn_id -> {el, text, frame}
  events: [],
  eventTimes: [],
  metrics: [],
  sessions: new Map(), // project_path -> session
  components: {},
  providers: {},
  settings: {},
  memories: [],
  project: null, // {name, path} — only what the core reports as active
  devOpen: false,
  ready: false,
  lastRoute: {},
  lastProvider: null,
  engaged: false,
  benchPinned: null, // null = follow the work; true/false = the user chose
  micPaused: false,
};
let entity = null;

// ------------------------------------------------------------------ entity

async function createEntity() {
  const canvas = $("entity");
  const { webglAvailable, Entity } = await import("./entity.js").catch((error) => ({ loadError: error }));
  if (Entity && webglAvailable()) {
    try {
      return new Entity(canvas, { reducedMotion });
    } catch (error) {
      console.warn("WebGL entity failed; using the 2D fallback", error);
      return fallback(canvas, String(error && error.message || error));
    }
  }
  return fallback(canvas, Entity ? "WebGL is unavailable" : "entity module failed to load");
}

async function fallback(canvas, reason) {
  // A canvas that already handed out a WebGL context can't give a 2D one: swap in a fresh element.
  const fresh = canvas.cloneNode(false);
  canvas.replaceWith(fresh);
  const { FallbackEntity } = await import("./fallback-entity.js");
  return new FallbackEntity(fresh, { reducedMotion, reason });
}

// The canvas fills the whole stage (particles drift behind the transcript); the entity itself is
// centred in the free area of the presence column, above the status line.
function alignEntity() {
  if (!entity) return;
  const stage = $("stage").getBoundingClientRect();
  const presence = document.querySelector(".presence").getBoundingClientRect();
  const statusTop = $("status").getBoundingClientRect().top;
  const zoneTop = presence.top;
  const zoneBottom = Math.max(zoneTop + 120, statusTop - 8);
  const x = presence.left + presence.width / 2 - (stage.left + stage.width / 2);
  const y = (zoneTop + zoneBottom) / 2 - (stage.top + stage.height / 2);
  const zone = Math.min(presence.width, zoneBottom - zoneTop);
  entity.setLayout({ x: Math.round(x), y: Math.round(y), zone: Math.round(zone) });
}

let holdTimer = 0;
function renderEntityState() {
  const { state, label } = tracker.snapshot();
  app.dataset.entity = state;
  if (entity) entity.setState(state);
  $("status").textContent = label;
  $("stopBtn").hidden = !["processing", "thinking", "executing", "speaking"].includes(state);
  // error and interruption are held briefly; re-evaluate when the hold lapses
  clearTimeout(holdTimer);
  const now = Date.now();
  const nextExpiry = [tracker.errorUntil, tracker.interruptedUntil].filter((t) => t > now).sort()[0];
  if (nextExpiry) holdTimer = setTimeout(renderEntityState, nextExpiry - now + 20);
}

// ------------------------------------------------------------------ connection

let socket = null;
let retry = 500;
let nextId = 1;

function connect() {
  socket = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
  socket.addEventListener("open", () => {
    retry = 500;
    tracker.setConnected(true);
    setLink();
    renderEntityState();
  });
  socket.addEventListener("message", (message) => {
    const payload = JSON.parse(message.data);
    if (payload.type === "snapshot") applySnapshot(payload.data);
    else if (payload.type === "events") payload.events.forEach(handleEvent);
    else if (payload.type === "reply" && payload.data && payload.data.components) applyRefresh(payload.data);
  });
  socket.addEventListener("close", () => {
    tracker.setConnected(false);
    setLink();
    renderEntityState();
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 8000);
  });
}

function send(cmd, data = {}) {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ cmd, id: nextId++, ...data }));
}

function setLink() {
  const link = $("link");
  if (!tracker.connected) {
    link.dataset.state = "offline";
    $("linkText").textContent = "Disconnected";
    return;
  }
  const claude = ui.components.claude_code;
  const degraded = Object.values(ui.components).some((c) => c.status === "error");
  link.dataset.state = degraded ? "degraded" : "online";
  $("linkText").textContent = claude && claude.status === "ready" ? "Claude Code linked"
    : claude && claude.status !== "loading" ? "Claude Code unavailable" : "Connected";
}

// ------------------------------------------------------------------ snapshot

function applySnapshot(data) {
  tracker.applySnapshot(data);
  ui.components = data.components || {};
  ui.providers = data.providers || {};
  ui.settings = data.settings || {};
  ui.memories = data.memories || [];
  ui.project = data.project || null;
  ui.sessions.clear();
  (data.sessions || []).forEach((s) => ui.sessions.set(s.project_path, s));
  renderComponents();
  renderSettings();
  renderMemories();
  setEngaged(data.engaged);
  setMic(data.mic_paused);
  renderProject();
  renderBench();
  if (data.pending_permission) showPermission(data.pending_permission);
  document.querySelectorAll("#captions .turn").forEach((el) => el.remove());
  for (const turn of data.history || []) {
    const el = addTurn(turn.role === "user" ? "user" : "assistant", turn.content);
    if (turn.role === "assistant" && turn.heard != null) el.classList.add("interrupted");
    if (turn.role === "assistant" && turn.meta && turn.meta.route === "rejected") el.classList.add("refusal");
  }
  markLatest();
  if (data.metrics) renderSummary(data.metrics);
  setLink();
  renderEntityState();
  if (Object.values(ui.components).length && !Object.values(ui.components).some((c) => c.status === "loading")) {
    finishBoot();
  }
}

function applyRefresh(data) {
  ui.components = data.components || ui.components;
  ui.providers = data.providers || ui.providers;
  ui.memories = data.memories || [];
  renderComponents();
  renderMemories();
  if (data.metrics) renderSummary(data.metrics);
  if (ui.devOpen) renderEngineering();
}

// ------------------------------------------------------------------ events

function handleEvent(event) {
  const { type, data } = event;
  countEvent(event);
  if (type !== "audio.level") tracker.apply(event);
  logEvent(event);
  switch (type) {
    case "audio.level":
      if (entity) entity.setLevels(data.mic || 0, data.out || 0);
      return;
    case "state.changed":
      if (data.state === "paused") setMic(true);
      else if (data.previous === "paused") setMic(false);
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
    case "assistant.delta":
      appendAssistant(data.turn_id, data.text);
      break;
    case "assistant.message":
      finishAssistant(data);
      break;
    case "announcement":
      addTurn("assistant", data.text).classList.add("announcement");
      markLatest();
      break;
    case "tool.start":
      if (entity && data.name === "claude.task") entity.pulse(0.8);
      break;
    case "permission.request":
      showPermission(data);
      break;
    case "permission.resolved":
    case "permission.expired":
      $("permission").hidden = true;
      break;
    case "coding.session":
      onSession(data);
      break;
    case "coding.progress":
      onProgress(data);
      break;
    case "coding.check":
      if (entity) entity.pulse(data.status === "failed" ? 1 : 0.6);
      break;
    case "project.active":
      ui.project = data.path ? { name: data.name, path: data.path } : null;
      renderProject();
      renderBench();
      break;
    case "system.status":
      ui.components[data.component] = { status: data.status, detail: data.detail };
      renderComponents();
      setLink();
      renderBench();
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
      ui.lastRoute = { route: data.route, reason: data.reason, category: data.category };
      break;
    case "llm.start":
      ui.lastRoute.provider = data.provider;
      ui.lastRoute.model = data.model;
      ui.lastProvider = `${data.provider}${data.model ? ` (${data.model})` : ""}`;
      break;
    case "conversation.engaged":
      setEngaged(data.engaged);
      break;
    case "audio.mic":
      setMic(data.status === "paused");
      if (data.status === "error") toast(`Microphone problem: ${data.error}`, true);
      break;
    case "error":
      toast(data.message || "Something failed.", true);
      break;
    case "memory.changed":
      send("snapshot");
      break;
    default:
      break;
  }
  if (type !== "assistant.delta") renderEntityState();
}

// ------------------------------------------------------------------ conversation

function addTurn(role, text) {
  $("captionsEmpty").hidden = true;
  const el = document.createElement("article");
  el.className = `turn ${role}`;
  const said = document.createElement("div");
  said.className = "said";
  if (role === "user") said.textContent = text;
  else said.innerHTML = renderMarkdown(text || "");
  el.appendChild(said);
  $("captions").appendChild(el);
  scrollCaptions();
  return el;
}

function appendAssistant(turnId, text) {
  let entry = ui.turns.get(turnId);
  if (!entry) {
    entry = { el: addTurn("assistant", ""), text: "", frame: 0 };
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
  let el;
  const entry = data.turn_id != null ? ui.turns.get(data.turn_id) : null;
  if (entry) {
    cancelAnimationFrame(entry.frame);
    entry.el.querySelector(".said").innerHTML = renderMarkdown(data.text || entry.text);
    el = entry.el;
    ui.turns.delete(data.turn_id);
  } else if (data.text) {
    el = addTurn("assistant", data.text);
  }
  if (el) {
    if (data.interrupted) el.classList.add("interrupted");
    if (data.route === "rejected") el.classList.add("refusal");
  }
  markLatest();
  scrollCaptions();
}

function markLatest() {
  const turns = [...document.querySelectorAll("#captions .turn")];
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
    const code = copy.closest(".code-block").querySelector("code").textContent;
    navigator.clipboard.writeText(code).then(() => {
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy"), 1500);
    });
    return;
  }
  const expand = e.target.closest(".expand");
  if (expand) {
    const block = expand.closest(".code-block");
    const collapsed = block.classList.toggle("collapsed");
    expand.textContent = collapsed ? `Show all ${block.dataset.lines} lines` : "Collapse";
    return;
  }
  const link = e.target.closest("a[href]");
  if (link) {
    e.preventDefault();
    send("open_url", { url: link.href });
  }
});

// ------------------------------------------------------------------ project & workbench

function renderProject() {
  const chip = $("projectChip");
  const active = !!ui.project;
  chip.dataset.active = String(active);
  $("projectName").textContent = active ? ui.project.name : NO_PROJECT;
  $("projectPath").textContent = active ? `Project directory ${ui.project.path}` : "No project selected";
  chip.title = active ? ui.project.path : "No project selected. Say “open <project>” or name one in a request.";
  const live = [...ui.sessions.values()].some((s) => s.status === "running");
  $("projectDot").className = "project-dot" + (live ? " live" : "");
}

function onSession(data) {
  const previous = ui.sessions.get(data.project_path);
  ui.sessions.set(data.project_path, { ...previous, ...data });
  if (data.status === "running" && previous?.status !== "running") ui.benchPinned = null; // a new task: follow it
  if (data.result_text && ["completed", "failed"].includes(data.status)) addTechnicalOutput(data);
  renderProject();
  renderBench();
}

function onProgress(data) {
  const session = data.project_path ? ui.sessions.get(data.project_path)
    : [...ui.sessions.values()].find((s) => s.project_name === data.project);
  if (session && data.kind === "tool") {
    session.activity = [...(session.activity || []), data.text].slice(-8);
    if (entity) entity.pulse(0.35);
    renderBench();
  }
}

function addTechnicalOutput(session) {
  const turn = addTurn("assistant", "");
  turn.classList.add("announcement");
  const details = document.createElement("details");
  details.className = "output";
  const summary = document.createElement("summary");
  summary.textContent = session.status === "failed"
    ? `Claude Code output for ${session.project_name} (failed)`
    : `Claude Code output for ${session.project_name}`;
  const body = document.createElement("div");
  body.className = "said";
  body.innerHTML = renderMarkdown(session.result_text);
  details.append(summary, body);
  turn.querySelector(".said").remove();
  turn.appendChild(details);
  if (session.status === "failed") turn.classList.add("error");
  markLatest();
}

function fact(list, label, value, cls) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  if (value instanceof Node) dd.appendChild(value);
  else dd.textContent = value;
  if (cls) dd.className = cls;
  list.append(dt, dd);
}

let benchTimer = 0;
function renderBench() {
  const view = buildWorkbench({ activeProject: ui.project, sessions: [...ui.sessions.values()],
    claude: ui.components.claude_code });
  const open = ui.benchPinned ?? view.expanded;
  app.dataset.bench = open ? "open" : "rail";
  $("benchToggle").setAttribute("aria-expanded", String(open));
  const dot = $("benchDot");
  dot.className = "bench-rail-dot" + (view.session?.statusKind === "live" ? " live"
    : view.session?.statusKind === "attention" ? " attention" : view.claude.state === "linked" ? " linked" : "");

  const facts = $("benchFacts");
  facts.innerHTML = "";
  fact(facts, "Project", view.projectLabel, view.hasProject ? "" : "muted");
  fact(facts, "Directory", view.projectPath || "None selected", view.projectPath ? "mono" : "muted");
  fact(facts, "Claude Code", view.claude.text, view.claude.state === "linked" ? "" : "muted");

  const task = $("benchTask");
  task.innerHTML = "";
  clearInterval(benchTimer);
  const s = view.session;
  if (!s) {
    const empty = document.createElement("p");
    empty.className = "task-mode";
    empty.textContent = view.hasProject ? "No task has run in this project yet."
      : "Select a project to give Claude Code a working directory.";
    task.appendChild(empty);
    if (view.hasProject) task.appendChild(clearProjectButton());
    requestAnimationFrame(alignEntity);
    return;
  }
  const box = document.createElement("section");
  box.className = `task ${s.statusKind}`;
  const head = document.createElement("div");
  head.className = "task-head";
  const mode = document.createElement("span");
  mode.className = "task-mode";
  mode.textContent = s.foreign ? `${s.mode} in ${s.project}` : s.mode;
  const status = document.createElement("span");
  status.className = `task-status ${s.statusKind}`;
  status.textContent = s.statusText;
  head.append(mode, status);
  box.appendChild(head);
  if (s.task) {
    const text = document.createElement("p");
    text.className = "task-text";
    text.textContent = s.task;
    box.appendChild(text);
  }
  const details = document.createElement("dl");
  details.className = "facts";
  fact(details, "Duration", s.duration || "Not measured", s.duration ? "" : "muted");
  fact(details, "Session", s.sessionId || "Not started", s.sessionId ? "mono" : "muted");
  for (const check of s.checks) {
    fact(details, check.label, `${capitalize(check.status)}: ${check.command}`, `check-${check.status}`);
  }
  if (!s.checks.length) fact(details, "Checks", "No tests or builds run yet", "muted");
  fact(details, "Cost", s.cost || "Not reported", s.cost ? "" : "muted");
  fact(details, "Tokens", s.tokens || "Not reported", s.tokens ? "" : "muted");
  box.appendChild(details);
  if (s.steps.length) {
    const steps = document.createElement("ol");
    steps.className = "steps";
    for (const line of s.steps) {
      const li = document.createElement("li");
      li.textContent = line;
      steps.appendChild(li);
    }
    box.appendChild(steps);
  }
  if (s.summary) {
    const summary = document.createElement("p");
    summary.className = "task-summary";
    summary.textContent = s.summary;
    box.appendChild(summary);
  }
  if (s.error) {
    const error = document.createElement("p");
    error.className = "task-error";
    error.textContent = s.error;
    box.appendChild(error);
  }
  const actions = document.createElement("div");
  actions.className = "task-actions";
  const action = (label, cmd) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn";
    b.textContent = label;
    b.addEventListener("click", () => send("coding", { action: cmd }));
    actions.appendChild(b);
  };
  if (s.status === "running") {
    action("Pause", "pause");
    action("Stop", "stop");
  } else if (["paused", "failed", "cancelled"].includes(s.status) && !s.foreign) {
    action("Resume", "resume");
  }
  if (actions.children.length) box.appendChild(actions);
  if (s.history.length > 1) {
    const title = document.createElement("h3");
    title.className = "bench-subtitle";
    title.textContent = "Earlier tasks";
    const list = document.createElement("ol");
    list.className = "history";
    for (const h of s.history.slice(1)) {
      const li = document.createElement("li");
      const what = document.createElement("span");
      what.className = "what";
      what.textContent = h.task;
      const meta = document.createElement("span");
      meta.textContent = [capitalize(h.status), h.duration, h.cost].filter(Boolean).join(", ");
      li.append(what, meta);
      list.appendChild(li);
    }
    box.append(title, list);
  }
  task.appendChild(box);
  if (view.hasProject && s.status !== "running") task.appendChild(clearProjectButton());
  if (s.status === "running") benchTimer = setInterval(renderBench, 1000);
  requestAnimationFrame(alignEntity);
}

function clearProjectButton() {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "btn";
  b.textContent = "Clear active project";
  b.addEventListener("click", () => send("project.clear"));
  return b;
}

$("benchToggle").addEventListener("click", () => {
  ui.benchPinned = app.dataset.bench !== "open";
  renderBench();
});
$("projectChip").addEventListener("click", () => {
  ui.benchPinned = true;
  renderBench();
});

function showPermission(request) {
  $("permission").hidden = false;
  $("permission").dataset.id = request.id;
  $("permissionText").textContent = `Confirmation required: ${request.action}.`;
}
$("permissionAllow").addEventListener("click", () => send("permission", { id: $("permission").dataset.id, approved: true }));
$("permissionDeny").addEventListener("click", () => send("permission", { id: $("permission").dataset.id, approved: false }));

// ------------------------------------------------------------------ health & boot

const COMPONENT_NAMES = {
  ui: "Interface", speaker: "Speaker", microphone: "Microphone", stt: "Speech recognition", tts: "Voice",
  projects: "Projects", apps: "Apps", computer: "Desktop control", claude_code: "Claude Code",
  "llm:freellm": "FreeLLMAPI", "llm:ollama": "Ollama", "llm:claude": "Claude",
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
    row.querySelector(".detail").textContent = `${info.status}${info.detail ? `: ${info.detail}` : ""}`;
    health.appendChild(row);
  }
  const providers = $("providers");
  providers.innerHTML = "";
  for (const [name, info] of Object.entries(ui.providers || {})) {
    const row = document.createElement("li");
    const ok = info.available !== undefined ? info.available : info.ok;
    row.className = ok ? "ok" : "down";
    row.innerHTML = `<span class="dot"></span><span class="name"></span><span class="detail"></span>`;
    row.querySelector(".name").textContent = `Model provider: ${name}`;
    row.querySelector(".detail").textContent = info.last_error || info.health || info.detail || (ok ? "available" : "unavailable");
    providers.appendChild(row);
  }
}

function finishBoot() {
  if (ui.ready) return;
  ui.ready = true;
  $("boot").classList.add("done");
  const problems = Object.entries(ui.components).filter(([, c]) => c.status === "error");
  if (problems.length) {
    toast(`Not started: ${problems.map(([n]) => COMPONENT_NAMES[n] || n).join(", ")}. Details are in telemetry.`, true);
  }
}

// ------------------------------------------------------------------ telemetry

function countEvent(event) {
  const now = performance.now();
  ui.eventTimes.push(now);
  while (ui.eventTimes.length && now - ui.eventTimes[0] > 10000) ui.eventTimes.shift();
}

function eventRate() {
  const now = performance.now();
  while (ui.eventTimes.length && now - ui.eventTimes[0] > 10000) ui.eventTimes.shift();
  return ui.eventTimes.length / 10;
}

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
    li.innerHTML = `<span class="time"></span><span class="type"></span><span class="data"></span>`;
    li.querySelector(".time").textContent = new Date(event.ts * 1000).toLocaleTimeString([], { hour12: false });
    li.querySelector(".type").textContent = event.type;
    li.querySelector(".data").textContent = JSON.stringify(event.data).slice(0, 300);
    list.appendChild(li);
  }
  $("eventRate").textContent = `${eventRate().toFixed(1)} events/s over the last 10 s`;
}

function renderEngineering() {
  const facts = $("engineeringFacts");
  facts.innerHTML = "";
  const claude = ui.components.claude_code;
  const running = [...ui.sessions.values()].filter((s) => s.status === "running");
  const view = buildWorkbench({ activeProject: ui.project, sessions: [...ui.sessions.values()], claude });
  fact(facts, "Claude Code", claude ? `${claude.status}${claude.detail ? `: ${claude.detail}` : ""}` : "Not checked yet");
  fact(facts, "Process", running.length ? `Running for ${running.map((s) => s.project_name).join(", ")}` : "No process running");
  fact(facts, "Project", view.projectLabel);
  fact(facts, "Session", view.session?.sessionId || "None", view.session?.sessionId ? "mono" : "muted");
  fact(facts, "Task status", view.session ? view.session.statusText : "No task", view.session ? "" : "muted");
  fact(facts, "Last provider", ui.lastProvider || "None used yet", ui.lastProvider ? "" : "muted");
  fact(facts, "Last route", ui.lastRoute.route ? `${ui.lastRoute.route}${ui.lastRoute.reason ? ` (${ui.lastRoute.reason})` : ""}` : "None yet");
  fact(facts, "Event bus", `${eventRate().toFixed(1)} events/s`);
  const info = entity ? entity.info() : null;
  fact(facts, "Entity", info ? `${info.backend}, ${info.fps || 0} fps, ${info.points} particles${info.running ? "" : ", paused"}` : "Not running");
}

function addMetrics(record) {
  ui.metrics.unshift({ ...record, route: { ...ui.lastRoute } });
  ui.metrics.length = Math.min(ui.metrics.length, 15);
  if (ui.devOpen) renderMetrics();
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
    tr.innerHTML = `<td></td><td></td><td></td>` + cell(metrics.endpoint_ms, 600, 1200) + cell(metrics.stt_wait_ms, 300, 800) +
      cell(metrics.llm_ttft_ms, 1500, 3000) + cell(metrics.tts_first_audio_ms, 300, 800) +
      cell(metrics.voice_to_voice_ms ?? metrics.text_to_voice_ms, 2000, 4000);
    tr.children[0].textContent = m.turn_id;
    tr.children[1].textContent = info.category || info.route || m.route.route || "—";
    tr.children[2].textContent = info.model || m.route.model || "—";
    body.appendChild(tr);
  }
  const last = ui.metrics[0];
  const box = $("lastTurn");
  box.innerHTML = "";
  if (!last) return;
  const fields = {
    Route: (last.info && last.info.route) || last.route.route,
    Category: last.info && last.info.category,
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

function renderSummary(summary) {
  const box = $("metricsSummary");
  box.innerHTML = "";
  const labels = { voice_to_voice_ms: "End to end", llm_ttft_ms: "LLM first token", stt_wait_ms: "STT",
    tts_first_audio_ms: "TTS first audio", endpoint_ms: "End of speech", interrupt_ms: "Interruption" };
  for (const [key, label] of Object.entries(labels)) {
    const s = summary[key];
    if (!s) continue;
    const div = document.createElement("div");
    div.innerHTML = `<span></span><strong></strong>`;
    div.querySelector("span").textContent = `${label}, p50 / p90 over ${s.count}`;
    div.querySelector("strong").textContent = `${Math.round(s.p50)} / ${Math.round(s.p90)} ms`;
    box.appendChild(div);
  }
}

function renderMemories() {
  const list = $("memories");
  list.innerHTML = "";
  if (!ui.memories.length) {
    list.innerHTML = `<li><span class="scope">Nothing remembered. Say “remember that this project uses pnpm”.</span></li>`;
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

let devTimer = 0;
function openDev(open) {
  ui.devOpen = open;
  $("devPanel").hidden = !open;
  $("devToggle").setAttribute("aria-expanded", String(open));
  clearInterval(devTimer);
  if (open) {
    renderEvents();
    renderMetrics();
    renderEngineering();
    send("snapshot");
    devTimer = setInterval(() => {
      renderEngineering();
      $("eventRate").textContent = `${eventRate().toFixed(1)} events/s over the last 10 s`;
    }, 1000);
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
  document.querySelectorAll('input[name="voiceProfile"]').forEach((r) => (r.checked = r.value === s.tts.voice_profile));
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
  r.addEventListener("change", () => send("setting", { key: "conversation.engagement", value: r.value })));
document.querySelectorAll('input[name="voiceProfile"]').forEach((r) =>
  r.addEventListener("change", () => send("setting", { key: "tts.voice_profile", value: r.value })));
$("newConversation").addEventListener("click", () => {
  send("new_conversation");
  document.querySelectorAll("#captions .turn").forEach((t) => t.remove());
  $("captionsEmpty").hidden = false;
});
$("settingsToggle").addEventListener("click", () => {
  const open = $("settingsPanel").hidden;
  $("settingsPanel").hidden = !open;
  $("settingsToggle").setAttribute("aria-expanded", String(open));
});

// ------------------------------------------------------------------ input

function setEngaged(engaged) {
  ui.engaged = !!engaged;
  $("engagement").textContent = ui.micPaused ? "Microphone off. Type to SID"
    : ui.engaged ? "Listening without the wake word" : "Say “Hey SID” or type";
}

function setMic(paused) {
  ui.micPaused = !!paused;
  $("micToggle").setAttribute("aria-pressed", paused ? "true" : "false");
  $("micToggle").title = paused ? "Turn the microphone on (Ctrl+M)" : "Turn the microphone off (Ctrl+M)";
  $("micLabel").textContent = paused ? "Microphone off" : "Microphone on";
  setEngaged(ui.engaged);
}

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
$("stage").addEventListener("click", (e) => {
  if (e.target === $("entity") || e.target.classList.contains("presence")) {
    if (entity) entity.pulse(0.6);
    $("input").focus();
  }
});

// ------------------------------------------------------------------ helpers

let toastTimer = 0;
function toast(text, fault = false) {
  const el = $("toast");
  el.textContent = text;
  el.classList.toggle("fault", fault);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 6000);
}

function capitalize(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

// Diagnostics for automated UI checks (read-only views of real state).
window.__sid = {
  entity: () => (entity ? entity.info() : null),
  state: () => tracker.snapshot(),
  bench: () => ({ mode: app.dataset.bench, project: $("projectName").textContent, ...buildWorkbench({
    activeProject: ui.project, sessions: [...ui.sessions.values()], claude: ui.components.claude_code }) }),
  status: () => $("status").textContent,
};

new ResizeObserver(() => alignEntity()).observe(document.querySelector(".presence"));
createEntity().then((created) => {
  entity = created;
  alignEntity();
  renderEntityState();
});
renderProject();
renderBench();
connect();
