// The render loop stops entirely while the window is hidden or minimised.
import { test } from "node:test";
import assert from "node:assert/strict";
import { RenderLoop } from "../../sugar/ui/web/render-loop.js";

function harness() {
  let queued = null;
  let clock = 0;
  const listeners = {};
  const doc = {
    hidden: false,
    addEventListener: (name, fn) => (listeners[name] = fn),
    removeEventListener: (name) => delete listeners[name],
  };
  const frames = [];
  const loop = new RenderLoop({
    onFrame: (dt) => frames.push(dt),
    raf: (fn) => { queued = fn; return 1; },
    caf: () => { queued = null; },
    doc,
    now: () => clock,
  });
  const step = (ms) => { clock += ms; const fn = queued; queued = null; if (fn) fn(); };
  const setHidden = (hidden) => { doc.hidden = hidden; listeners.visibilitychange?.(); };
  return { loop, frames, step, setHidden, pending: () => queued !== null, listeners };
}

test("runs frames with a delta in seconds", () => {
  const h = harness();
  h.loop.start();
  h.step(16);
  h.step(16);
  assert.equal(h.frames.length, 2);
  assert.ok(Math.abs(h.frames[0] - 0.016) < 1e-9);
});

test("hidden window: no frames are scheduled; restore resumes with a clamped delta", () => {
  const h = harness();
  h.loop.start();
  h.step(16);
  h.setHidden(true);
  assert.equal(h.loop.running, false);
  assert.equal(h.pending(), false);
  h.step(60000); // a minute minimised
  assert.equal(h.frames.length, 1);
  h.setHidden(false);
  assert.equal(h.loop.running, true);
  h.step(5000);
  assert.ok(h.frames[1] <= 0.1, "the first frame after restore never jumps");
});

test("starting while hidden waits for visibility", () => {
  const h = harness();
  h.setHidden(true);
  h.loop.start();
  assert.equal(h.pending(), false);
  h.setHidden(false);
  assert.equal(h.pending(), true);
});

test("stop removes the listener and cancels the frame", () => {
  const h = harness();
  h.loop.start();
  h.loop.stop();
  assert.equal(h.pending(), false);
  assert.equal(h.listeners.visibilitychange, undefined);
});
