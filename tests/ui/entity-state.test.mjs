// Entity state derivation: every state comes from a real event, never from a guess.
import { test } from "node:test";
import assert from "node:assert/strict";
import { StateTracker, deriveState, PRESETS, LABELS, STATES, easeParams } from "../../sugar/ui/web/entity-state.js";

const ev = (type, data = {}) => ({ type, data });

function online() {
  const t = new StateTracker();
  t.applySnapshot({ state: "idle", sessions: [] });
  return t;
}

test("every state has a preset and a label", () => {
  for (const s of STATES) {
    assert.ok(PRESETS[s], `preset for ${s}`);
    assert.ok(LABELS[s], `label for ${s}`);
  }
});

test("offline until connected, offline again when the socket drops", () => {
  const t = new StateTracker();
  assert.equal(t.snapshot().state, "offline");
  t.applySnapshot({ state: "idle", sessions: [] });
  assert.deepEqual(t.snapshot(), { state: "idle", label: "Awaiting input" });
  t.setConnected(false);
  assert.equal(t.snapshot().state, "offline");
});

test("foreground states map from the core state machine", () => {
  const t = online();
  const cases = { listening: "listening", transcribing: "processing", tool_execution: "executing",
    speaking: "speaking", paused: "muted", error: "error" };
  for (const [backend, expected] of Object.entries(cases)) {
    t.apply(ev("state.changed", { state: backend }), 0);
    assert.equal(t.snapshot(0).state, expected, backend);
  }
});

test("thinking starts as processing and becomes thinking only once a model is answering", () => {
  const t = online();
  t.apply(ev("turn.started", { turn_id: "a" }));
  t.apply(ev("state.changed", { state: "thinking" }));
  assert.equal(t.snapshot().state, "processing");
  assert.equal(t.snapshot().label, "Analysing request");
  t.apply(ev("llm.start", { provider: "freellm" }));
  assert.equal(t.snapshot().state, "thinking");
  assert.equal(t.snapshot().label, "Engineering solution");
  t.apply(ev("assistant.message", { turn_id: "a" }));
  t.apply(ev("state.changed", { state: "idle" }));
  assert.equal(t.snapshot().state, "idle");
});

test("coding only while a Claude Code session reports running", () => {
  const t = online();
  t.apply(ev("coding.session", { project_path: "D:/p/shop", project_name: "shop", status: "completed" }));
  assert.equal(t.snapshot().state, "idle", "a finished session is not coding");
  t.apply(ev("coding.session", { project_path: "D:/p/shop", project_name: "shop", status: "running" }));
  assert.deepEqual(t.snapshot(), { state: "coding", label: "Engineering in shop" });
  t.apply(ev("state.changed", { state: "speaking" }));
  assert.equal(t.snapshot().state, "speaking", "foreground speech outranks background coding");
  t.apply(ev("state.changed", { state: "idle" }));
  t.apply(ev("coding.session", { project_path: "D:/p/shop", project_name: "shop", status: "failed" }));
  assert.equal(t.snapshot().state, "idle");
});

test("a stale running session in the snapshot is still a real running process", () => {
  const t = new StateTracker();
  t.applySnapshot({ state: "idle", sessions: [{ project_path: "x", project_name: "x", status: "paused" }] });
  assert.equal(t.snapshot().state, "idle");
});

test("speaking requires the core to say it is speaking", () => {
  const t = online();
  t.apply(ev("audio.level", { out: 0.9 }));
  assert.notEqual(t.snapshot().state, "speaking");
});

test("interruption is held briefly, then follows the core", () => {
  const t = online();
  t.apply(ev("state.changed", { state: "speaking" }), 1000);
  t.apply(ev("barge_in", { latency_ms: 20 }), 1000);
  t.apply(ev("state.changed", { state: "listening" }), 1010);
  assert.equal(t.snapshot(1100).state, "interrupted");
  assert.equal(t.snapshot(2000).state, "listening");
});

test("errors pulse for a moment with a specific message", () => {
  const t = online();
  t.apply(ev("error", { source: "stt", message: "x" }), 0);
  assert.deepEqual(t.snapshot(100), { state: "error", label: "Speech recognition failed" });
  assert.equal(t.snapshot(5000).state, "idle");
});

test("deriveState precedence", () => {
  const base = { connected: true, backend: "idle", phase: null, codingProject: null, errorActive: false, interruptedActive: false };
  assert.equal(deriveState({ ...base, connected: false, errorActive: true }), "offline");
  assert.equal(deriveState({ ...base, errorActive: true, backend: "speaking" }), "error");
  assert.equal(deriveState({ ...base, codingProject: "x", backend: "tool_execution" }), "executing");
});

test("easeParams converges without allocating", () => {
  const current = { ...PRESETS.idle };
  const target = PRESETS.executing;
  for (let i = 0; i < 300; i++) assert.equal(easeParams(current, target, 1 / 60), current);
  assert.ok(Math.abs(current.speed - target.speed) < 1e-3);
});
