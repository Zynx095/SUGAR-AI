// SID's entity: a real-time WebGL particle system (Three.js r169, vendored).
//
//   core      emissive sphere with shader noise, a tight hot glow and a wide soft glow (bloom without
//             a post-processing pass)
//   rings     five inclined elliptical orbits; ~1,500 particles each, moved entirely on the GPU, with
//             travelling bright "packets" that read as energy trails, plus a faint guide line
//   field     ~14,000 particles in a flattened shell: organic turbulence, inward pull, radial waves
//   streams   fine horizontal particle lanes flowing out of the core
//   wave      a radial energy pulse (camera-facing ring) that also lifts the field as it passes
//
// Every animated quantity is a uniform: per frame the CPU only eases ~16 numbers toward the
// current state preset (entity-state.js) and writes them — no allocation, no buffer uploads.
// The render loop stops while the window is hidden or minimised (render-loop.js).

import * as THREE from "./vendor/three.module.min.js";
import { PRESETS, easeParams } from "./entity-state.js";
import { RenderLoop } from "./render-loop.js";

const TAU = Math.PI * 2;
const AMBER = new THREE.Color("#ffab3d");
const GOLD = new THREE.Color("#f6cf7a");
const EMBER = new THREE.Color("#ff6a12");
const WARN = new THREE.Color("#ff4524");

const NOISE = /* glsl */ `
  vec3 hash3(vec3 p) {
    p = vec3(dot(p, vec3(127.1, 311.7, 74.7)), dot(p, vec3(269.5, 183.3, 246.1)), dot(p, vec3(113.5, 271.9, 124.6)));
    return -1.0 + 2.0 * fract(sin(p) * 43758.5453123);
  }
  float noise3(vec3 p) {
    vec3 i = floor(p), f = fract(p);
    vec3 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(mix(dot(hash3(i), f), dot(hash3(i + vec3(1,0,0)), f - vec3(1,0,0)), u.x),
                   mix(dot(hash3(i + vec3(0,1,0)), f - vec3(0,1,0)), dot(hash3(i + vec3(1,1,0)), f - vec3(1,1,0)), u.x), u.y),
               mix(mix(dot(hash3(i + vec3(0,0,1)), f - vec3(0,0,1)), dot(hash3(i + vec3(1,0,1)), f - vec3(1,0,1)), u.x),
                   mix(dot(hash3(i + vec3(0,1,1)), f - vec3(0,1,1)), dot(hash3(i + vec3(1,1,1)), f - vec3(1,1,1)), u.x), u.y), u.z);
  }
`;

const POINT_FRAGMENT = /* glsl */ `
  varying vec3 vColor;
  varying float vAlpha;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    float d = dot(c, c) * 4.0;
    if (d > 1.0) discard;
    float a = vAlpha * (1.0 - d) * (1.0 - d);
    gl_FragColor = vec4(vColor * a, a);
  }
`;

function rng(seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

export class Entity {
  constructor(canvas, { reducedMotion = false } = {}) {
    this.canvas = canvas;
    this.reducedMotion = reducedMotion;
    this.state = "offline";
    this.target = { ...PRESETS.offline };
    this.params = { ...PRESETS.offline };
    this.level = 0; // smoothed audio level that drives the core
    this.mic = 0;
    this.out = 0;
    this.phase = 0; // integrated orbital phase (speed changes never jump)
    this.time = 0;
    this.nextPulse = 3;
    this.wave = { r: 0, amp: 0 };
    this.pointer = { x: 0, y: 0, tx: 0, ty: 0 };
    this.layout = { x: 0, y: 0, zone: 0 }; // where the entity sits inside the full-stage canvas (CSS px)
    this.contextLost = false;
    this.backend = "webgl";
    this._build();
    this.loop = new RenderLoop({ onFrame: (dt) => this._frame(dt) });
    this._onPointer = this._onPointer.bind(this);
    this._onLost = this._onLost.bind(this);
    this._onRestored = this._onRestored.bind(this);
    window.addEventListener("pointermove", this._onPointer, { passive: true });
    canvas.addEventListener("webglcontextlost", this._onLost);
    canvas.addEventListener("webglcontextrestored", this._onRestored);
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(canvas.parentElement || canvas);
    this.resize();
    this.loop.start();
  }

  // ------------------------------------------------------------------ public

  setState(state) {
    if (!PRESETS[state] || state === this.state) return;
    const previous = this.state;
    this.state = state;
    this.target = { ...PRESETS[state] };
    if (state === "interrupted") {
      // immediate drop in activity, not an eased one
      this.params.speed = PRESETS.interrupted.speed;
      this.params.core = Math.min(this.params.core, 0.5);
    }
    if (!this.reducedMotion && previous !== "offline" && ["processing", "coding", "executing", "error"].includes(state)) {
      this.pulse(state === "error" ? 1 : 0.7);
    }
  }

  setLevels(mic, out) {
    this.mic = Math.max(0, Math.min(1, mic * 4));
    this.out = Math.max(0, Math.min(1, out * 4));
  }

  /** A radial energy wave from the core (state changes, coding steps, clicks). */
  pulse(strength = 0.7) {
    if (this.reducedMotion) return;
    this.wave.r = 0.55;
    this.wave.amp = Math.min(1.2, Math.max(this.wave.amp, strength));
  }

  /**
   * Centre the entity on a point of the stage (offsets from the canvas centre, CSS px) and size it to
   * fit a zone of ``zone`` px (the smaller side of the free area above the status line).
   */
  setLayout({ x = 0, y = 0, zone = 0 } = {}) {
    this.layout = { x, y, zone };
    this.resize();
  }

  resize() {
    const parent = this.canvas.parentElement || this.canvas;
    const width = Math.max(1, parent.clientWidth);
    const height = Math.max(1, parent.clientHeight);
    const ratio = Math.min(window.devicePixelRatio || 1, 1.75);
    this.renderer.setPixelRatio(ratio);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    // Distance chosen so the outer orbits fill the free zone, whatever the canvas size.
    const zone = this.layout.zone > 0 ? this.layout.zone : Math.min(width, height);
    this.camera.position.z = Math.min(32, Math.max(7, (7.7 * height) / zone));
    this.camera.setViewOffset(width, height, -this.layout.x, -this.layout.y, width, height);
    this.camera.updateProjectionMatrix();
    this.uniforms.uPixelRatio.value = ratio;
    this.uniforms.uViewport.value = height * ratio;
  }

  info() {
    return { backend: this.backend, state: this.state, running: this.loop.running, frames: this.loop.frames,
      fps: this.loop.fps, contextLost: this.contextLost, points: this.pointCount,
      size: [this.renderer.domElement.width, this.renderer.domElement.height] };
  }

  dispose() {
    this.loop.stop();
    this.resizeObserver.disconnect();
    window.removeEventListener("pointermove", this._onPointer);
    this.canvas.removeEventListener("webglcontextlost", this._onLost);
    this.canvas.removeEventListener("webglcontextrestored", this._onRestored);
    this.scene.traverse((object) => {
      object.geometry?.dispose();
      if (object.material) [].concat(object.material).forEach((m) => m.dispose());
    });
    this.renderer.dispose();
  }

  // ------------------------------------------------------------------ construction

  _build() {
    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: false, alpha: false,
      powerPreference: "high-performance", preserveDrawingBuffer: false });
    this.renderer.setClearColor(0x030201, 1);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(36, 1, 0.1, 100);
    this.camera.position.set(0, 0, 9.2);
    this.group = new THREE.Group();
    this.scene.add(this.group);
    this.uniforms = {
      uTime: { value: 0 }, uPhase: { value: 0 }, uCore: { value: 0.8 }, uLevel: { value: 0 },
      uTurbulence: { value: 0.25 }, uInward: { value: 0 }, uStructure: { value: 0.4 }, uRings: { value: 0.5 },
      uField: { value: 0.5 }, uStreams: { value: 0.3 }, uWarn: { value: 0 }, uDim: { value: 1 },
      uFlicker: { value: 0 }, uWaveR: { value: 0 }, uWaveAmp: { value: 0 }, uPixelRatio: { value: 1 },
      uViewport: { value: 800 }, uAmber: { value: AMBER.clone() }, uGold: { value: GOLD.clone() },
      uEmber: { value: EMBER.clone() }, uWarnColor: { value: WARN.clone() },
    };
    this.pointCount = 0;
    this._buildCore();
    this._buildRings();
    this._buildField();
    this._buildStreams();
    this._buildWave();
  }

  _material(vertexShader, fragmentShader, extra = {}) {
    return new THREE.ShaderMaterial({ uniforms: this.uniforms, vertexShader, fragmentShader, transparent: true,
      depthWrite: false, blending: THREE.AdditiveBlending, ...extra });
  }

  _buildCore() {
    const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.5, 24), this._material(/* glsl */ `
      uniform float uTime; uniform float uLevel; uniform float uFlicker;
      varying vec3 vNormal; varying vec3 vPos; varying vec3 vView;
      ${NOISE}
      void main() {
        float n = noise3(position * 2.6 + vec3(0.0, uTime * 0.45, uTime * 0.2));
        vec3 p = position + normal * (n * (0.035 + 0.05 * uFlicker) + uLevel * 0.06);
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        vNormal = normalize(normalMatrix * normal);
        vPos = position;
        vView = normalize(-mv.xyz);
        gl_Position = projectionMatrix * mv;
      }`, /* glsl */ `
      uniform float uTime; uniform float uCore; uniform float uLevel; uniform float uWarn; uniform float uDim;
      uniform float uFlicker; uniform vec3 uAmber; uniform vec3 uGold; uniform vec3 uEmber; uniform vec3 uWarnColor;
      varying vec3 vNormal; varying vec3 vPos; varying vec3 vView;
      ${NOISE}
      void main() {
        float facing = clamp(dot(vNormal, vView), 0.0, 1.0);
        float rim = pow(1.0 - facing, 2.2);
        float bands = noise3(vPos * 3.4 + vec3(uTime * 0.35, -uTime * 0.22, uTime * 0.18));
        float veins = smoothstep(0.08, 0.5, abs(noise3(vPos * 6.0 - uTime * 0.5)));
        float flick = 1.0 + uFlicker * 0.25 * sin(uTime * 23.0 + bands * 9.0);
        vec3 white = vec3(1.0, 0.95, 0.84);
        vec3 col = uEmber * (0.6 + 0.7 * bands) + uGold * pow(facing, 1.5) * (0.45 + 0.6 * veins)
                 + white * pow(facing, 6.0) * 0.75 + uAmber * rim * 0.9;
        col = mix(col, uWarnColor * (0.8 + 0.6 * facing), uWarn * 0.75);
        float edge = smoothstep(0.0, 0.45, facing);  // soft limb: plasma, not a hard planet edge
        float intensity = (0.6 + 0.8 * uCore + 0.9 * uLevel) * flick * uDim;
        gl_FragColor = vec4(col * intensity * edge, edge);
      }`));
    this.core = core;
    this.group.add(core);

    // Two camera-facing glows stand in for a bloom pass: a tight hot one and a wide soft one.
    const glowShader = (tightness, strength) => this._material(/* glsl */ `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        vec4 mv = modelViewMatrix * vec4(0.0, 0.0, 0.0, 1.0);
        mv.xy += position.xy;
        gl_Position = projectionMatrix * mv;
      }`, /* glsl */ `
      uniform float uCore; uniform float uLevel; uniform float uWarn; uniform float uDim; uniform float uTime;
      uniform vec3 uAmber; uniform vec3 uGold; uniform vec3 uWarnColor;
      varying vec2 vUv;
      void main() {
        float r = length(vUv - 0.5) * 2.0;
        float g = exp(-r * r * ${tightness.toFixed(2)}) * (1.0 - smoothstep(0.85, 1.0, r));
        float breathe = 0.92 + 0.08 * sin(uTime * 1.3);
        vec3 col = mix(mix(uAmber, uGold, g), uWarnColor, uWarn * 0.7);
        float a = g * ${strength.toFixed(2)} * (0.35 + 0.6 * uCore + 0.8 * uLevel) * breathe * uDim;
        gl_FragColor = vec4(col * a, a);
      }`);
    const tight = new THREE.Mesh(new THREE.PlaneGeometry(3.2, 3.2), glowShader(7.0, 1.55));
    const wide = new THREE.Mesh(new THREE.PlaneGeometry(10.5, 10.5), glowShader(5.0, 0.5));
    tight.renderOrder = 2;
    wide.renderOrder = 1;
    this.group.add(wide, tight);
  }

  _buildRings() {
    const random = rng(7);
    const rings = [
      { rx: 1.55, ry: 1.12, tiltX: 1.18, tiltZ: 0.18, speed: 0.55, count: 1500, packets: 5 },
      { rx: 2.05, ry: 1.38, tiltX: 1.32, tiltZ: -0.42, speed: -0.38, count: 1700, packets: 4 },
      { rx: 2.6, ry: 1.7, tiltX: 1.05, tiltZ: 0.62, speed: 0.27, count: 1800, packets: 6 },
      { rx: 3.15, ry: 1.95, tiltX: 1.22, tiltZ: -0.1, speed: -0.2, count: 1600, packets: 3 },
      { rx: 1.9, ry: 1.9, tiltX: 0.35, tiltZ: 1.2, speed: 0.33, count: 1200, packets: 4 },
    ];
    this.rings = [];
    const vertex = /* glsl */ `
      uniform float uPhase; uniform float uTime; uniform float uStructure; uniform float uRings; uniform float uLevel;
      uniform float uPixelRatio; uniform float uViewport; uniform float uWaveR; uniform float uWaveAmp; uniform float uDim;
      uniform float uWarn; uniform vec3 uAmber; uniform vec3 uGold; uniform vec3 uWarnColor;
      uniform float rx; uniform float ry; uniform float speed; uniform float packets;
      attribute float aAngle; attribute float aJitter; attribute float aSize; attribute float aSeed;
      varying vec3 vColor; varying float vAlpha;
      void main() {
        float ang = aAngle + uPhase * speed;
        float scatter = mix(0.16, 0.025, uStructure) * aJitter;
        vec3 p = vec3(cos(ang) * (rx + scatter), sin(ang) * (ry + scatter), scatter * 0.6 * sin(aSeed * 40.0));
        float travel = fract(aAngle / ${TAU.toFixed(6)} * packets - uPhase * speed * 0.35);
        float trail = pow(travel, mix(7.0, 14.0, uStructure));
        float wave = exp(-pow(length(p) - uWaveR, 2.0) * 6.0) * uWaveAmp;
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = aSize * (1.0 + trail * 1.6 + wave) * uPixelRatio * (uViewport / 900.0) * (9.0 / -mv.z);
        vColor = mix(mix(uAmber, uGold, trail), uWarnColor, uWarn * 0.6);
        vAlpha = (0.34 + trail * 1.1 + wave * 0.7 + uLevel * 0.2) * uRings * uDim;
      }`;
    for (const spec of rings) {
      const geometry = new THREE.BufferGeometry();
      const n = spec.count;
      const angle = new Float32Array(n), jitter = new Float32Array(n), size = new Float32Array(n), seed = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        angle[i] = (i / n) * TAU + random() * 0.01;
        jitter[i] = (random() - 0.5) * 2 * Math.pow(random(), 1.5);
        size[i] = 1.7 + random() * 2.6;
        seed[i] = random();
      }
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(n * 3), 3));
      geometry.setAttribute("aAngle", new THREE.BufferAttribute(angle, 1));
      geometry.setAttribute("aJitter", new THREE.BufferAttribute(jitter, 1));
      geometry.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
      geometry.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
      geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 4);
      const material = this._material(vertex, POINT_FRAGMENT);
      material.uniforms = { ...this.uniforms, rx: { value: spec.rx }, ry: { value: spec.ry },
        speed: { value: spec.speed }, packets: { value: spec.packets } };
      const points = new THREE.Points(geometry, material);
      const pivot = new THREE.Group();
      pivot.rotation.set(spec.tiltX, 0, spec.tiltZ);
      pivot.add(points);

      // the faint guide line of the orbit
      const curve = new THREE.EllipseCurve(0, 0, spec.rx, spec.ry, 0, TAU);
      const line = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(curve.getPoints(180)),
        new THREE.LineBasicMaterial({ color: AMBER, transparent: true, opacity: 0.08, blending: THREE.AdditiveBlending,
          depthWrite: false }));
      pivot.add(line);
      this.group.add(pivot);
      this.rings.push({ pivot, points, line, spec, drift: random() * TAU });
      this.pointCount += n;
    }
  }

  _buildField() {
    const random = rng(21);
    const n = 14000;
    const position = new Float32Array(n * 3), size = new Float32Array(n), seed = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      // flattened shell, denser near the core, with depth spread
      const r = 1.05 + Math.pow(random(), 1.7) * 3.6;
      const theta = random() * TAU;
      const y = (random() * 2 - 1) * (0.25 + 0.55 * random());
      const flat = Math.sqrt(Math.max(0, 1 - y * y));
      position[i * 3] = Math.cos(theta) * flat * r;
      position[i * 3 + 1] = y * r * 0.62;
      position[i * 3 + 2] = Math.sin(theta) * flat * r;
      size[i] = 1.2 + Math.pow(random(), 3) * 4.2;
      seed[i] = random();
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(position, 3));
    geometry.setAttribute("aSize", new THREE.BufferAttribute(size, 1));
    geometry.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
    geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 6);
    const material = this._material(/* glsl */ `
      uniform float uPhase; uniform float uTime; uniform float uTurbulence; uniform float uInward; uniform float uField;
      uniform float uPixelRatio; uniform float uViewport; uniform float uWaveR; uniform float uWaveAmp; uniform float uDim;
      uniform float uLevel; uniform float uWarn; uniform float uFlicker; uniform vec3 uAmber; uniform vec3 uGold;
      uniform vec3 uWarnColor;
      attribute float aSize; attribute float aSeed;
      varying vec3 vColor; varying float vAlpha;
      void main() {
        vec3 p = position;
        float r = length(p.xz);
        float spin = uPhase * (0.16 + 0.5 / max(r, 0.8));
        float c = cos(spin), s = sin(spin);
        p.xz = mat2(c, -s, s, c) * p.xz;
        float t = uTime * (0.35 + aSeed * 0.4);
        p += vec3(sin(p.y * 1.7 + t + aSeed * 6.0), sin(p.z * 1.3 + t * 1.1), sin(p.x * 1.5 + t * 0.9)) * uTurbulence * 0.16;
        float pull = uInward * (0.5 + 0.5 * sin(uTime * 1.6 + aSeed * 6.2831));
        p *= 1.0 - pull * 0.32;
        float d = length(p);
        float wave = exp(-pow(d - uWaveR, 2.0) * 5.0) * uWaveAmp;
        p += normalize(p + 1e-4) * wave * 0.22;
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_Position = projectionMatrix * mv;
        float depth = clamp((-mv.z - 5.0) / 8.0, 0.0, 1.0);
        gl_PointSize = aSize * uPixelRatio * (uViewport / 900.0) * (9.0 / -mv.z) * (1.0 + wave * 0.8);
        float twinkle = 0.65 + 0.35 * sin(uTime * (1.0 + aSeed * 3.0) + aSeed * 40.0);
        float flick = 1.0 + uFlicker * 0.6 * sin(uTime * 11.0 + aSeed * 90.0);
        vColor = mix(mix(uAmber, uGold, aSeed * 0.6), uWarnColor, uWarn * 0.55);
        vAlpha = (0.42 + 0.8 * twinkle + wave * 1.3 + uLevel * 0.3) * (1.0 - depth * 0.55) * flick * uField * uDim
                 * smoothstep(5.2, 2.0, d);
      }`, POINT_FRAGMENT);
    this.field = new THREE.Points(geometry, material);
    this.field.rotation.x = 0.22;
    this.group.add(this.field);
    this.pointCount += n;
  }

  _buildStreams() {
    const random = rng(33);
    const n = 2600;
    const position = new Float32Array(n * 3), seed = new Float32Array(n), lane = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      seed[i] = random();
      lane[i] = (Math.round((random() * 2 - 1) * 9) / 9) * 0.55 * Math.pow(random(), 0.6);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(position, 3));
    geometry.setAttribute("aSeed", new THREE.BufferAttribute(seed, 1));
    geometry.setAttribute("aLane", new THREE.BufferAttribute(lane, 1));
    geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 9);
    const material = this._material(/* glsl */ `
      uniform float uPhase; uniform float uStreams; uniform float uStructure; uniform float uPixelRatio;
      uniform float uViewport; uniform float uDim; uniform float uWarn; uniform vec3 uAmber; uniform vec3 uGold;
      uniform vec3 uWarnColor;
      attribute float aSeed; attribute float aLane;
      varying vec3 vColor; varying float vAlpha;
      void main() {
        float dir = aSeed > 0.5 ? 1.0 : -1.0;
        float progress = fract(aSeed * 7.31 + uPhase * (0.22 + aSeed * 0.18));
        float x = dir * (0.7 + progress * 6.2);
        vec3 p = vec3(x, aLane * (1.0 - progress * 0.35), (aSeed - 0.5) * 0.35);
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_Position = projectionMatrix * mv;
        gl_PointSize = (1.3 + aSeed * 1.6) * uPixelRatio * (uViewport / 900.0) * (9.0 / -mv.z);
        vColor = mix(mix(uGold, uAmber, progress), uWarnColor, uWarn * 0.5);
        vAlpha = sin(progress * 3.14159) * (1.0 - progress * 0.7) * 1.5 * uStreams * uDim * mix(0.6, 1.0, uStructure);
      }`, POINT_FRAGMENT);
    this.streams = new THREE.Points(geometry, material);
    this.group.add(this.streams);
    this.pointCount += n;
  }

  _buildWave() {
    const material = this._material(/* glsl */ `
      varying vec2 vUv;
      void main() {
        vUv = uv;
        vec4 mv = modelViewMatrix * vec4(0.0, 0.0, 0.0, 1.0);
        mv.xy += position.xy;
        gl_Position = projectionMatrix * mv;
      }`, /* glsl */ `
      uniform float uWaveR; uniform float uWaveAmp; uniform float uDim; uniform float uWarn;
      uniform vec3 uAmber; uniform vec3 uWarnColor;
      varying vec2 vUv;
      void main() {
        float r = length(vUv - 0.5) * 12.0;
        float band = exp(-pow(r - uWaveR, 2.0) * 22.0) * uWaveAmp * smoothstep(5.8, 3.0, uWaveR);
        vec3 col = mix(uAmber, uWarnColor, uWarn * 0.7);
        gl_FragColor = vec4(col * band * 0.35 * uDim, band * 0.35 * uDim);
      }`);
    this.waveMesh = new THREE.Mesh(new THREE.PlaneGeometry(12, 12), material);
    this.waveMesh.renderOrder = 3;
    this.group.add(this.waveMesh);
  }

  // ------------------------------------------------------------------ frame

  _frame(dt) {
    if (this.contextLost) return;
    const motion = this.reducedMotion ? 0.35 : 1;
    const p = easeParams(this.params, this.target, dt, this.state === "interrupted" ? 9 : 3.2);
    // audio: fast attack, slow release, only from the source this state reacts to
    const source = this.target.react === "mic" ? this.mic : this.target.react === "out" ? this.out : 0;
    const k = source > this.level ? 1 - Math.exp(-18 * dt) : 1 - Math.exp(-4 * dt);
    this.level += (source - this.level) * k;

    this.time += dt * motion;
    this.phase += dt * p.speed * motion;
    if (p.pulse > 0 && !this.reducedMotion) {
      this.nextPulse -= dt;
      if (this.nextPulse <= 0) {
        this.pulse(this.state === "error" ? 1 : 0.55);
        this.nextPulse = p.pulse * (0.8 + Math.random() * 0.4);
      }
    }
    if (this.wave.amp > 0.001) {
      this.wave.r += dt * (2.4 + this.wave.r * 0.6);
      this.wave.amp *= Math.exp(-dt * 0.9);
      if (this.wave.r > 6) this.wave.amp = 0;
    }

    const u = this.uniforms;
    u.uTime.value = this.time;
    u.uPhase.value = this.phase;
    u.uCore.value = p.core;
    u.uLevel.value = this.level;
    u.uTurbulence.value = p.turbulence;
    u.uInward.value = p.inward;
    u.uStructure.value = p.structure;
    u.uRings.value = p.rings;
    u.uField.value = p.field;
    u.uStreams.value = p.streams;
    u.uWarn.value = p.warn;
    u.uDim.value = p.dim;
    u.uFlicker.value = p.flicker;
    u.uWaveR.value = this.wave.r;
    u.uWaveAmp.value = this.wave.amp;

    // independent ring precession, a slow turn of the whole entity and a pointer-driven tilt
    for (const ring of this.rings) {
      ring.pivot.rotation.y = ring.drift + this.phase * 0.08 * Math.sign(ring.spec.speed);
      ring.line.material.opacity = 0.03 + 0.09 * p.rings * p.structure * p.dim;
    }
    this.pointer.x += (this.pointer.tx - this.pointer.x) * (1 - Math.exp(-3 * dt));
    this.pointer.y += (this.pointer.ty - this.pointer.y) * (1 - Math.exp(-3 * dt));
    this.group.rotation.y = this.time * 0.05 + this.pointer.x * 0.35;
    this.group.rotation.x = -0.12 + this.pointer.y * 0.22;
    this.core.scale.setScalar(1 + this.level * 0.12 + Math.sin(this.time * 1.4) * 0.015 * p.core);

    this.renderer.render(this.scene, this.camera);
  }

  // ------------------------------------------------------------------ events

  _onPointer(event) {
    this.pointer.tx = (event.clientX / window.innerWidth) * 2 - 1;
    this.pointer.ty = (event.clientY / window.innerHeight) * 2 - 1;
  }

  _onLost(event) {
    event.preventDefault(); // allow the browser to restore the context
    this.contextLost = true;
    this.loop.stop();
  }

  _onRestored() {
    this.contextLost = false;
    this.loop.start();
  }
}
