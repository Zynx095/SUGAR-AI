// The orb: a ball of spun sugar that shows what Sugar is doing.
//   idle        slow breathing, frosted
//   listening   mint, the rim ripples with the microphone level
//   thinking    lilac, the sugar crystals spiral inward
//   working     honey, a steady pulse while a tool runs
//   speaking    pink sugar, ripples with Sugar's own voice
//   paused      grey and still (mic off)
// A thin honey orbit appears while Claude Code is working in the background.

const STATE_COLORS = {
  idle: [237, 230, 255],
  listening: [140, 240, 208],
  transcribing: [140, 240, 208],
  thinking: [183, 156, 255],
  tool_execution: [255, 200, 107],
  speaking: [255, 143, 184],
  interrupted: [237, 230, 255],
  error: [255, 122, 122],
  paused: [111, 103, 130],
};

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

export class Orb {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.state = "idle";
    this.color = [...STATE_COLORS.idle];
    this.target = [...STATE_COLORS.idle];
    this.micLevel = 0;
    this.outLevel = 0;
    this.level = 0;
    this.coding = false;
    this.codingFade = 0;
    this.flash = 0;
    this.t = 0;
    this.crystals = Array.from({ length: 46 }, (_, i) => ({
      angle: (i / 46) * Math.PI * 2 + Math.random() * 0.4,
      radius: 0.55 + Math.random() * 0.5,
      speed: 0.12 + Math.random() * 0.25,
      size: 0.6 + Math.random() * 1.6,
      phase: Math.random() * Math.PI * 2,
    }));
    this._resize = this._resize.bind(this);
    window.addEventListener("resize", this._resize);
    this._resize();
    this._last = performance.now();
    requestAnimationFrame((now) => this._frame(now));
  }

  setState(state) {
    if (state === "interrupted") this.flash = 1;
    this.state = state;
    this.target = [...(STATE_COLORS[state] || STATE_COLORS.idle)];
  }

  setLevels(mic, out) {
    this.micLevel = mic;
    this.outLevel = out;
  }

  setCoding(active) {
    this.coding = active;
  }

  _resize() {
    const ratio = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(rect.width * ratio));
    this.canvas.height = Math.max(1, Math.round(rect.height * ratio));
    this.size = Math.min(this.canvas.width, this.canvas.height);
  }

  _frame(now) {
    const dt = Math.min(0.05, (now - this._last) / 1000);
    this._last = now;
    this.t += dt;
    const ease = 1 - Math.pow(0.002, dt);
    for (let i = 0; i < 3; i++) this.color[i] += (this.target[i] - this.color[i]) * ease;
    const wanted = this.state === "speaking" ? this.outLevel
      : (this.state === "listening" || this.state === "transcribing") ? this.micLevel
      : 0;
    this.level += (wanted - this.level) * (1 - Math.pow(0.0005, dt));
    this.codingFade += ((this.coding ? 1 : 0) - this.codingFade) * ease;
    this.flash = Math.max(0, this.flash - dt * 3);
    this._draw();
    requestAnimationFrame((n) => this._frame(n));
  }

  _rgba(alpha, lift = 0) {
    const [r, g, b] = this.color.map((c) => Math.round(c + (255 - c) * lift));
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  _draw() {
    const { ctx, canvas } = this;
    const w = canvas.width;
    const h = canvas.height;
    const cx = w / 2;
    const cy = h / 2;
    const still = reduceMotion.matches;
    const t = still ? 0 : this.t;
    const R = this.size * 0.24;
    const breathe = this.state === "idle" ? Math.sin(t * 1.1) * 0.025
      : this.state === "tool_execution" ? Math.sin(t * 4) * 0.03
      : this.state === "paused" ? 0 : Math.sin(t * 1.7) * 0.015;
    const energy = Math.min(1, this.level * 1.6);
    ctx.clearRect(0, 0, w, h);

    // Outer glow.
    const glow = ctx.createRadialGradient(cx, cy, R * 0.6, cx, cy, R * 2.05);
    glow.addColorStop(0, this._rgba(0.22 + energy * 0.2));
    glow.addColorStop(1, this._rgba(0));
    ctx.fillStyle = glow;
    ctx.beginPath();
    ctx.arc(cx, cy, R * 2.05, 0, Math.PI * 2);
    ctx.fill();

    // The sugar-glass body: a rim that ripples with the voice.
    const points = 120;
    ctx.beginPath();
    for (let i = 0; i <= points; i++) {
      const a = (i / points) * Math.PI * 2;
      const ripple =
        Math.sin(a * 3 + t * 1.3) * 0.018 +
        Math.sin(a * 5 - t * 2.1) * 0.012 * (0.4 + energy * 3) +
        Math.sin(a * 9 + t * 5.3) * 0.02 * energy +
        Math.sin(a * 13 - t * 7.7) * 0.012 * energy;
      const r = R * (1 + breathe + ripple + energy * 0.06);
      const x = cx + Math.cos(a) * r;
      const y = cy + Math.sin(a) * r;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    }
    ctx.closePath();
    const body = ctx.createRadialGradient(cx - R * 0.35, cy - R * 0.4, R * 0.1, cx, cy, R * 1.1);
    body.addColorStop(0, this._rgba(0.95, 0.65));
    body.addColorStop(0.35, this._rgba(0.75, 0.15));
    body.addColorStop(1, this._rgba(0.18));
    ctx.fillStyle = body;
    ctx.fill();
    ctx.lineWidth = Math.max(1, this.size * 0.004);
    ctx.strokeStyle = this._rgba(0.5, 0.4);
    ctx.stroke();

    // Facets: slow glints refracting inside the glass.
    ctx.save();
    ctx.clip();
    ctx.globalCompositeOperation = "lighter";
    for (let i = 0; i < 4; i++) {
      const a = t * (0.12 + i * 0.05) + i * 1.7;
      const fx = cx + Math.cos(a) * R * 0.35;
      const fy = cy + Math.sin(a * 1.3) * R * 0.3;
      const facet = ctx.createRadialGradient(fx, fy, 0, fx, fy, R * (0.55 - i * 0.08));
      facet.addColorStop(0, this._rgba(0.18, 0.8));
      facet.addColorStop(1, this._rgba(0));
      ctx.fillStyle = facet;
      ctx.fillRect(cx - R * 1.2, cy - R * 1.2, R * 2.4, R * 2.4);
    }
    ctx.restore();

    // Sugar crystals around the orb.
    const thinking = this.state === "thinking" || this.state === "transcribing";
    for (const c of this.crystals) {
      const spin = still ? 0 : t * c.speed * (thinking ? 3.2 : 1);
      const pull = thinking ? 0.85 + 0.15 * Math.sin(t * 2 + c.phase) : 1;
      const reach = R * (1.15 + c.radius * 0.45 * pull + energy * 0.25 * Math.sin(c.phase + t * 3));
      const a = c.angle + spin;
      const x = cx + Math.cos(a) * reach;
      const y = cy + Math.sin(a) * reach * 0.92;
      const twinkle = 0.35 + 0.65 * Math.abs(Math.sin(t * 1.5 + c.phase));
      const s = c.size * (this.size / 300);
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(a + c.phase);
      ctx.fillStyle = this._rgba(this.state === "paused" ? 0.15 : 0.25 + twinkle * 0.45, 0.5);
      ctx.fillRect(-s, -s, s * 2, s * 2);
      ctx.restore();
    }

    // Claude Code orbit.
    if (this.codingFade > 0.01) {
      const orbit = R * 1.62;
      ctx.save();
      ctx.globalAlpha = this.codingFade;
      ctx.strokeStyle = "rgba(255, 200, 107, 0.35)";
      ctx.setLineDash([2 * (this.size / 300), 7 * (this.size / 300)]);
      ctx.lineWidth = Math.max(1, this.size * 0.003);
      ctx.beginPath();
      ctx.ellipse(cx, cy, orbit, orbit * 0.32, -0.35, 0, Math.PI * 2);
      ctx.stroke();
      const a = t * 0.9;
      const sx = cx + Math.cos(a) * orbit * Math.cos(-0.35) - Math.sin(a) * orbit * 0.32 * Math.sin(-0.35);
      const sy = cy + Math.cos(a) * orbit * Math.sin(-0.35) + Math.sin(a) * orbit * 0.32 * Math.cos(-0.35);
      ctx.setLineDash([]);
      ctx.fillStyle = "rgba(255, 200, 107, 0.95)";
      ctx.beginPath();
      ctx.arc(sx, sy, Math.max(2, this.size * 0.012), 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    // Interruption flash.
    if (this.flash > 0) {
      ctx.strokeStyle = `rgba(237, 230, 255, ${this.flash * 0.8})`;
      ctx.lineWidth = this.size * 0.01;
      ctx.beginPath();
      ctx.arc(cx, cy, R * (1.1 + (1 - this.flash) * 0.6), 0, Math.PI * 2);
      ctx.stroke();
    }
  }
}
