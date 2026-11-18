// Frame scheduling for the entity: pauses while the window is hidden or minimised
// (no GPU or CPU work at all), clamps the frame delta after a pause, and counts
// frames for the telemetry panel. Pure: browser APIs are injected, so it is
// unit-tested under Node (tests/ui/render-loop.test.mjs).

export class RenderLoop {
  constructor({ onFrame, raf = globalThis.requestAnimationFrame?.bind(globalThis),
                caf = globalThis.cancelAnimationFrame?.bind(globalThis), doc = globalThis.document,
                now = () => performance.now() }) {
    this.onFrame = onFrame;
    this.raf = raf;
    this.caf = caf;
    this.doc = doc;
    this.now = now;
    this.handle = 0;
    this.active = false; // start() was called and stop() wasn't
    this.last = 0;
    this.frames = 0;
    this.fps = 0;
    this._fpsWindow = { start: 0, frames: 0 };
    this._tick = this._tick.bind(this);
    this._onVisibility = this._onVisibility.bind(this);
  }

  get hidden() {
    return !!(this.doc && this.doc.hidden);
  }

  /** True while frames are actually being scheduled. */
  get running() {
    return this.handle !== 0;
  }

  start() {
    if (this.active) return;
    this.active = true;
    this.doc?.addEventListener?.("visibilitychange", this._onVisibility);
    if (!this.hidden) this._schedule();
  }

  stop() {
    this.active = false;
    this.doc?.removeEventListener?.("visibilitychange", this._onVisibility);
    this._cancel();
  }

  _schedule() {
    if (this.handle) return;
    this.last = this.now();
    this._fpsWindow = { start: this.last, frames: 0 };
    this.handle = this.raf(this._tick);
  }

  _cancel() {
    if (this.handle) this.caf(this.handle);
    this.handle = 0;
  }

  _onVisibility() {
    if (!this.active) return;
    if (this.hidden) this._cancel();
    else this._schedule();
  }

  _tick() {
    this.handle = 0;
    if (!this.active || this.hidden) return;
    const now = this.now();
    const dt = Math.min(0.1, Math.max(0, (now - this.last) / 1000)); // never jump after a stall
    this.last = now;
    this.frames += 1;
    const w = this._fpsWindow;
    w.frames += 1;
    if (now - w.start >= 1000) {
      this.fps = Math.round((w.frames * 1000) / (now - w.start));
      w.start = now;
      w.frames = 0;
    }
    this.onFrame(dt, now);
    if (this.active && !this.hidden) this.handle = this.raf(this._tick);
  }
}
