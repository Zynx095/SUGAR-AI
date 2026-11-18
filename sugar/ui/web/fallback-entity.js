// Canvas 2D stand-in for the entity when WebGL is unavailable (driver blocklist, remote desktop,
// lost context that never comes back). Same interface and the same state presets as entity.js,
// a few hundred particles instead of twenty thousand.

import { PRESETS, easeParams } from "./entity-state.js";
import { RenderLoop } from "./render-loop.js";

const TAU = Math.PI * 2;

export class FallbackEntity {
  constructor(canvas, { reducedMotion = false, reason = "" } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.reducedMotion = reducedMotion;
    this.reason = reason;
    this.backend = "canvas2d";
    this.state = "offline";
    this.target = { ...PRESETS.offline };
    this.params = { ...PRESETS.offline };
    this.level = 0;
    this.mic = 0;
    this.out = 0;
    this.phase = 0;
    this.time = 0;
    this.layout = { x: 0, y: 0, zone: 0 };
    this.colours = ["#ffab3d", "#f6cf7a", "#ff6a12", "#ff4524"];
    this.wave = 0;
    this.particles = Array.from({ length: 420 }, (_, i) => ({
      ring: i % 4, angle: Math.random() * TAU, jitter: (Math.random() - 0.5) * 0.08, size: 0.6 + Math.random() * 1.6,
    }));
    this.loop = new RenderLoop({ onFrame: (dt) => this._frame(dt) });
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas.parentElement || canvas);
    this.resize();
    this.loop.start();
  }

  setState(state) {
    if (!PRESETS[state] || state === this.state) return;
    this.state = state;
    this.target = { ...PRESETS[state] };
  }

  setLevels(mic, out) {
    this.mic = Math.min(1, mic * 4);
    this.out = Math.min(1, out * 4);
  }

  pulse() {
    if (!this.reducedMotion) this.wave = 0.01;
  }

  setPalette(colours) {
    if (colours && colours.length >= 3) this.colours = [...colours.slice(0, 3), colours[3] || "#ff9a3c"];
  }

  setLayout({ x = 0, y = 0, zone = 0 } = {}) {
    this.layout = { x, y, zone };
  }

  resize() {
    const parent = this.canvas.parentElement || this.canvas;
    const ratio = Math.min(window.devicePixelRatio || 1, 1.75);
    this.ratio = ratio;
    this.canvas.width = Math.max(1, Math.round(parent.clientWidth * ratio));
    this.canvas.height = Math.max(1, Math.round(parent.clientHeight * ratio));
  }

  info() {
    return { backend: this.backend, reason: this.reason, state: this.state, running: this.loop.running,
      frames: this.loop.frames, fps: this.loop.fps, points: this.particles.length,
      size: [this.canvas.width, this.canvas.height] };
  }

  dispose() {
    this.loop.stop();
    this.resizeObserver.disconnect();
  }

  _frame(dt) {
    const p = easeParams(this.params, this.target, dt);
    const source = this.target.react === "mic" ? this.mic : this.target.react === "out" ? this.out : 0;
    this.level += (source - this.level) * (1 - Math.exp(-(source > this.level ? 18 : 4) * dt));
    const motion = this.reducedMotion ? 0.35 : 1;
    this.time += dt * motion;
    this.phase += dt * p.speed * motion;
    if (this.wave > 0) this.wave = this.wave > 1 ? 0 : this.wave + dt * 0.6;

    const { ctx, canvas } = this;
    const w = canvas.width;
    const h = canvas.height;
    const cx = w / 2 + this.layout.x * this.ratio;
    const cy = h / 2 + this.layout.y * this.ratio;
    const R = (this.layout.zone > 0 ? this.layout.zone * this.ratio : Math.min(w, h)) * 0.1;
    ctx.fillStyle = "#000000";
    ctx.fillRect(0, 0, w, h);
    ctx.globalCompositeOperation = "lighter";
    const [body, , , warning] = this.colours;
    const rgba = (hex, a) => `rgba(${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}, ${a})`;
    const warm = (a) => rgba(p.warn > 0.5 ? warning : body, a);

    const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 5);
    glow.addColorStop(0, warm(0.35 * p.dim * (0.6 + p.core * 0.5 + this.level)));
    glow.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = glow;
    ctx.fillRect(cx - R * 5, cy - R * 5, R * 10, R * 10);

    const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * (1 + this.level * 0.2));
    core.addColorStop(0, `rgba(255, 214, 218, ${0.95 * p.dim})`);
    core.addColorStop(0.5, warm(0.9 * p.dim * p.core));
    core.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 1.3, 0, TAU);
    ctx.fill();

    const radii = [[2.2, 0.7, 0.4], [3.0, 0.9, -0.5], [3.8, 1.2, 0.2], [2.6, 2.6, 1.1]];
    for (const particle of this.particles) {
      const [rx, ry, tilt] = radii[particle.ring];
      const a = particle.angle + this.phase * (particle.ring % 2 ? -0.5 : 0.6);
      const x0 = Math.cos(a) * R * (rx + particle.jitter);
      const y0 = Math.sin(a) * R * (ry + particle.jitter);
      const x = cx + x0 * Math.cos(tilt) - y0 * Math.sin(tilt);
      const y = cy + x0 * Math.sin(tilt) + y0 * Math.cos(tilt);
      ctx.fillStyle = warm((0.25 + 0.5 * p.rings) * p.dim);
      ctx.fillRect(x, y, particle.size * this.ratio, particle.size * this.ratio);
    }
    if (this.wave > 0) {
      ctx.strokeStyle = warm((1 - this.wave) * 0.4);
      ctx.lineWidth = 2 * this.ratio;
      ctx.beginPath();
      ctx.arc(cx, cy, R * (1.2 + this.wave * 4), 0, TAU);
      ctx.stroke();
    }
    ctx.globalCompositeOperation = "source-over";
  }
}
