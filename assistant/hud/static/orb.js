// Nova's bubble: a sphere of glowing particles that turns slowly, breathes with the voice it
// hears, ripples while it speaks and swirls while it thinks, inside two orbiting rings.
// One shared simulation (colour, level, motion) drives any number of canvases.
"use strict";

const NovaOrb = (() => {
  const N = 900;
  const GOLDEN = Math.PI * (3 - Math.sqrt(5));
  const PTS = [];
  for (let i = 0; i < N; i++) {                       // evenly spread points on a sphere
    const y = 1 - (i / (N - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const th = i * GOLDEN;
    PTS.push([Math.cos(th) * r, y, Math.sin(th) * r, Math.random()]);
  }
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const SPIN = { thinking: 1.9, speaking: 0.9, listening: 0.55, standby: 0.12, offline: 0.06, muted: 0.2 };
  const AMP = { thinking: 0.05, speaking: 0.07, listening: 0.05, confirm: 0.04, standby: 0.01, offline: 0.005, muted: 0.012 };
  const GLOW = { standby: 0.25, offline: 0.12, muted: 0.35 };

  const sim = { c: [70, 180, 230], lvl: 0, amp: 0.02, glow: 0.6, t: 0, rot: 0, swirl: 0, talk: 0, state: "idle" };

  function step(state, level, color) {
    sim.state = state;
    sim.c = sim.c.map((v, i) => v + (color[i] - v) * 0.07);
    const heard = state === "listening" || state === "idle" || state === "dictation" ? level : 0;
    sim.lvl += (heard - sim.lvl) * 0.22;
    const speed = reduced ? 0.25 : 1;
    sim.t += 0.016 * speed;
    sim.rot += 0.004 * speed * (SPIN[state] ?? 0.4) * 2.2;
    sim.swirl += ((state === "thinking" ? 1 : 0) - sim.swirl) * 0.05;
    sim.talk += ((state === "speaking" ? 1 : 0) - sim.talk) * 0.08;
    sim.amp += ((AMP[state] ?? 0.025) + sim.lvl * 0.35 - sim.amp) * 0.12;
    sim.glow += ((GLOW[state] ?? 0.6 + sim.lvl * 0.5 + sim.talk * 0.15) - sim.glow) * 0.06;
  }

  function draw(cv) {
    if (!cv || !cv.offsetParent) return;               // hidden page: skip
    const dpr = window.devicePixelRatio || 1;
    const size = Math.min(cv.clientWidth, cv.clientHeight);
    if (!size) return;
    if (cv.width !== Math.round(cv.clientWidth * dpr) || cv.height !== Math.round(cv.clientHeight * dpr)) {
      cv.width = Math.round(cv.clientWidth * dpr);
      cv.height = Math.round(cv.clientHeight * dpr);
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, cv.clientWidth, cv.clientHeight);
    const cx = cv.clientWidth / 2, cy = cv.clientHeight / 2;
    const R = size * 0.3 * (1 + sim.lvl * 0.18);
    const [r, gr, b] = sim.c.map(Math.round);
    const t = sim.t;
    const rgba = (a) => `rgba(${r},${gr},${b},${a})`;
    const lift = (v, k) => Math.round(v + (255 - v) * k);

    // halo
    const halo = g.createRadialGradient(cx, cy, R * 0.5, cx, cy, R * 2.1);
    halo.addColorStop(0, rgba(0.3 * sim.glow));
    halo.addColorStop(0.5, rgba(0.08 * sim.glow));
    halo.addColorStop(1, rgba(0));
    g.fillStyle = halo;
    g.fillRect(0, 0, cv.clientWidth, cv.clientHeight);

    // inner core
    const core = g.createRadialGradient(cx - R * 0.2, cy - R * 0.25, 0, cx, cy, R);
    core.addColorStop(0, `rgba(${lift(r, 0.9)},${lift(gr, 0.9)},${lift(b, 0.9)},${0.7 * sim.glow})`);
    core.addColorStop(0.3, rgba(0.36 * sim.glow));
    core.addColorStop(1, rgba(0.02));
    g.fillStyle = core;
    g.beginPath();
    g.arc(cx, cy, R * 0.98, 0, Math.PI * 2);
    g.fill();

    // rings (behind half first, particles, then front half)
    const rings = [
      { rx: 1.42, ry: 0.3, rot: -0.35, speed: 0.6, dash: [60, 240], w: 1.6, a: 0.6 },
      { rx: 1.6, ry: 0.4, rot: 0.55, speed: -0.4, dash: [30, 170], w: 1.3, a: 0.45 },
    ];
    const ring = (o, front) => {
      g.save();
      g.translate(cx, cy);
      g.rotate(o.rot + Math.sin(t * 0.3) * 0.05);
      const from = front ? 0 : Math.PI, to = front ? Math.PI : Math.PI * 2;
      g.strokeStyle = rgba(0.12 + 0.1 * sim.glow);          // the ring itself: thin and faint
      g.lineWidth = 1;
      g.beginPath();
      g.ellipse(0, 0, R * o.rx, R * o.ry, 0, from, to);
      g.stroke();
      g.setLineDash(o.dash);                                  // a bright arc running along it
      g.lineDashOffset = -t * 60 * o.speed * (1 + sim.swirl * 2.5);
      g.strokeStyle = rgba(o.a * (0.35 + sim.glow));
      g.lineWidth = o.w;
      g.lineCap = "round";
      g.beginPath();
      g.ellipse(0, 0, R * o.rx, R * o.ry, 0, from, to);
      g.stroke();
      if (front) {                                     // a bright node travelling the ring
        const a = (t * o.speed * (1.2 + sim.swirl * 3)) % (Math.PI * 2);
        const nx = Math.cos(a) * R * o.rx, ny = Math.sin(a) * R * o.ry;
        if (Math.sin(a) > 0) {
          g.setLineDash([]);
          g.fillStyle = `rgba(${lift(r, 0.7)},${lift(gr, 0.7)},${lift(b, 0.7)},${0.9 * sim.glow + 0.1})`;
          g.shadowColor = rgba(1);
          g.shadowBlur = 12;
          g.beginPath();
          g.arc(nx, ny, 2.4, 0, Math.PI * 2);
          g.fill();
          g.shadowBlur = 0;
        }
      }
      g.restore();
    };
    for (const o of rings) ring(o, false);

    // particles
    g.globalCompositeOperation = "lighter";
    const ay = sim.rot, ax = 0.38 + Math.sin(t * 0.25) * 0.12;
    const cosY = Math.cos(ay), sinY = Math.sin(ay), cosX = Math.cos(ax), sinX = Math.sin(ax);
    const dotScale = size / 320;
    for (const p of PTS) {
      let [x, y, z, s] = p;
      // swirl: latitude-dependent twist while thinking
      if (sim.swirl > 0.01) {
        const tw = sim.swirl * Math.sin(t * 2 + y * 3) * 0.6;
        const c = Math.cos(tw), sn = Math.sin(tw);
        [x, z] = [x * c - z * sn, x * sn + z * c];
      }
      const x1 = x * cosY - z * sinY, z1 = x * sinY + z * cosY;
      const y1 = y * cosX - z1 * sinX, z2 = y * sinX + z1 * cosX;
      const wob = Math.sin(3 * x + t * 1.7) * Math.sin(2.5 * y + t * 1.3) +
                  sim.talk * Math.sin(9 * y - t * 10) * 0.8 + Math.sin(s * 40 + t * 3) * 0.25;
      const d = 1 + sim.amp * wob;
      const f = 2.6 / (2.6 - z2);
      const px = cx + x1 * R * d * f, py = cy + y1 * R * d * f;
      const depth = (z2 + 1) / 2;
      const a = (0.08 + 0.8 * depth * depth) * (0.45 + 0.55 * sim.glow);
      const k = 0.25 + 0.5 * depth;
      g.fillStyle = `rgba(${lift(r, k)},${lift(gr, k)},${lift(b, k)},${a})`;
      const sz = (0.7 + 1.5 * depth) * dotScale * (s > 0.97 ? 1.8 : 1);
      g.fillRect(px - sz / 2, py - sz / 2, sz, sz);
    }
    g.globalCompositeOperation = "source-over";
    for (const o of rings) ring(o, true);
  }

  const canvases = new Set();
  let get = () => ({ state: "idle", level: 0, color: [70, 180, 230] });
  function frame() {
    const { state, level, color } = get();
    step(state, level, color);
    for (const cv of canvases) draw(cv);
    requestAnimationFrame(frame);
  }
  return {
    add(cv) { if (cv) canvases.add(cv); },
    start(source) { get = source; requestAnimationFrame(frame); },
  };
})();
