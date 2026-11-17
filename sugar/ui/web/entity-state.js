// SID's entity state, derived only from real application events.
//
// Nothing here is simulated: the entity shows CODING only while a Claude Code
// session reports "running", SPEAKING only while the core reports the speaking
// state, EXECUTING only while a foreground tool runs. Pure module (no DOM, no
// WebGL) so it is unit-tested under Node: tests/ui/entity-state.test.mjs.

export const STATES = [
  "idle", "listening", "processing", "thinking", "coding", "executing",
  "speaking", "interrupted", "error", "offline", "muted",
];

// Visual targets for the particle entity. The renderer eases toward them.
//   speed      orbital/field angular speed      core      core brightness
//   turbulence organic field motion             inward    pull of the field toward the core
//   structure  ring precision vs. scatter       rings     ring brightness
//   field      particle-field brightness        streams   horizontal stream brightness
//   pulse      seconds between energy waves (0 = none)   warn  red-orange warning mix
//   dim        global brightness                flicker   energy fluctuation
//   react      how strongly audio level drives the core ("mic" | "out" | null)
export const PRESETS = {
  idle:        { speed: 0.22, core: 0.82, turbulence: 0.25, inward: 0.0,  structure: 0.35, rings: 0.55, field: 0.5,  streams: 0.3,  pulse: 9,   warn: 0, dim: 1.0,  flicker: 0.0,  react: null },
  listening:   { speed: 0.34, core: 0.9,  turbulence: 0.5,  inward: 0.0,  structure: 0.3,  rings: 0.65, field: 0.7,  streams: 0.45, pulse: 0,   warn: 0, dim: 1.0,  flicker: 0.0,  react: "mic" },
  processing:  { speed: 0.95, core: 0.95, turbulence: 0.35, inward: 0.6,  structure: 0.45, rings: 0.75, field: 0.75, streams: 0.55, pulse: 0,   warn: 0, dim: 1.0,  flicker: 0.15, react: null },
  thinking:    { speed: 0.6,  core: 1.0,  turbulence: 0.95, inward: 0.25, structure: 0.25, rings: 0.7,  field: 0.85, streams: 0.5,  pulse: 4.5, warn: 0, dim: 1.0,  flicker: 0.55, react: null },
  coding:      { speed: 0.72, core: 1.0,  turbulence: 0.12, inward: 0.1,  structure: 1.0,  rings: 1.0,  field: 0.6,  streams: 0.95, pulse: 3.5, warn: 0, dim: 1.0,  flicker: 0.0,  react: null },
  executing:   { speed: 1.15, core: 1.05, turbulence: 0.3,  inward: 0.15, structure: 0.85, rings: 1.0,  field: 0.75, streams: 1.0,  pulse: 1.2, warn: 0, dim: 1.0,  flicker: 0.1,  react: null },
  speaking:    { speed: 0.4,  core: 0.85, turbulence: 0.35, inward: 0.0,  structure: 0.5,  rings: 0.7,  field: 0.65, streams: 0.5,  pulse: 0,   warn: 0, dim: 1.0,  flicker: 0.0,  react: "out" },
  interrupted: { speed: 0.06, core: 0.45, turbulence: 0.05, inward: 0.0,  structure: 0.3,  rings: 0.3,  field: 0.3,  streams: 0.1,  pulse: 0,   warn: 0, dim: 0.8,  flicker: 0.0,  react: null },
  error:       { speed: 0.3,  core: 0.9,  turbulence: 0.4,  inward: 0.1,  structure: 0.4,  rings: 0.6,  field: 0.55, streams: 0.2,  pulse: 1.4, warn: 1, dim: 1.0,  flicker: 0.2,  react: null },
  offline:     { speed: 0.04, core: 0.28, turbulence: 0.08, inward: 0.0,  structure: 0.3,  rings: 0.18, field: 0.2,  streams: 0.0,  pulse: 0,   warn: 0, dim: 0.35, flicker: 0.0,  react: null },
  muted:       { speed: 0.12, core: 0.55, turbulence: 0.15, inward: 0.0,  structure: 0.35, rings: 0.35, field: 0.35, streams: 0.15, pulse: 0,   warn: 0, dim: 0.6,  flicker: 0.0,  react: null },
};

export const LABELS = {
  idle: "Awaiting input",
  listening: "Listening",
  processing: "Analysing request",
  thinking: "Engineering solution",
  coding: "Engineering",
  executing: "Executing task",
  speaking: "Speaking",
  interrupted: "Listening",
  error: "Fault detected",
  offline: "Offline. Reconnecting",
  muted: "Microphone off",
};

const ERROR_HOLD_MS = 2600;
const INTERRUPT_HOLD_MS = 650;
const ERROR_SOURCES = { stt: "Speech recognition failed", llm: "No model answered", turn: "The request failed",
  tts: "Voice synthesis failed", claude: "Claude Code failed" };

/** Tracks the facts that decide the entity state. Feed it bus events; read snapshot(now). */
export class StateTracker {
  constructor() {
    this.connected = false;
    this.backend = "idle"; // AssistantState from the core
    this.phase = null; // null | "processing" | "thinking" — progress of the current foreground turn
    this.turnId = null;
    this.running = new Map(); // project_path -> project_name, for sessions that report "running"
    this.errorUntil = 0;
    this.errorText = "";
    this.interruptedUntil = 0;
  }

  setConnected(connected) {
    this.connected = !!connected;
    if (!connected) this.phase = null;
  }

  /** Initial state from a snapshot message. */
  applySnapshot(data) {
    this.connected = true;
    if (data.state) this.backend = data.state;
    this.running.clear();
    for (const s of data.sessions || []) if (s.status === "running") this.running.set(s.project_path, s.project_name);
  }

  apply(event, now = Date.now()) {
    const { type, data = {} } = event;
    switch (type) {
      case "state.changed":
        this.backend = data.state;
        if (data.state === "idle" || data.state === "listening") this.phase = null;
        if (data.state === "interrupted") this.interruptedUntil = now + INTERRUPT_HOLD_MS;
        break;
      case "turn.started":
        this.turnId = data.turn_id;
        this.phase = "processing";
        break;
      case "llm.start":
      case "llm.first_token":
        if (this.phase) this.phase = "thinking";
        break;
      case "assistant.message":
      case "turn.cancelled":
        if (!data.turn_id || data.turn_id === this.turnId) this.phase = null;
        break;
      case "coding.session":
        if (data.status === "running") this.running.set(data.project_path, data.project_name);
        else this.running.delete(data.project_path);
        break;
      case "barge_in":
        this.interruptedUntil = now + INTERRUPT_HOLD_MS;
        this.phase = null;
        break;
      case "error":
        this.errorUntil = now + ERROR_HOLD_MS;
        this.errorText = ERROR_SOURCES[data.source] || "Fault detected";
        break;
      default:
        break;
    }
  }

  get codingProject() {
    const names = [...this.running.values()];
    return names.length ? names[names.length - 1] : null;
  }

  /** The entity state and its status line at time ``now``. */
  snapshot(now = Date.now()) {
    const s = deriveState({
      connected: this.connected,
      backend: this.backend,
      phase: this.phase,
      codingProject: this.codingProject,
      errorActive: now < this.errorUntil,
      interruptedActive: now < this.interruptedUntil,
    });
    let label = LABELS[s];
    if (s === "coding") label = `Engineering in ${this.codingProject}`;
    if (s === "error") label = this.errorText || LABELS.error;
    return { state: s, label };
  }
}

/** Precedence: connection → fault → interruption → foreground activity → background Claude Code → idle. */
export function deriveState({ connected, backend, phase, codingProject, errorActive, interruptedActive }) {
  if (!connected) return "offline";
  if (backend === "error" || errorActive) return "error";
  if (backend === "interrupted" || interruptedActive) return "interrupted";
  if (backend === "paused") return "muted";
  if (backend === "speaking") return "speaking";
  if (backend === "listening") return "listening";
  if (backend === "transcribing") return "processing";
  if (backend === "tool_execution") return "executing";
  if (backend === "thinking") return phase === "thinking" ? "thinking" : "processing";
  if (codingProject) return "coding";
  return "idle";
}

/** Exponential easing of numeric preset values toward a target (no allocation). */
export function easeParams(current, target, dt, rate = 3.2) {
  const k = 1 - Math.exp(-rate * dt);
  for (const key in target) {
    const value = target[key];
    if (typeof value === "number") current[key] += (value - current[key]) * k;
  }
  return current;
}
