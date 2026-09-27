// Nova's brain view: a glowing brain in the middle, every memory around it grouped by topic,
// pulses when a memory is used. Memory text is only ever drawn on the canvas or set with
// textContent. Shares globals with hud.js (S, COLORS, send, $, reduced).
"use strict";

const NovaBrain = (() => {
  const STOP = new Set(("a an the and or but if of to in on at for with about is are was were be been am i me my mine " +
    "you your it its this that what when where who how do does did can could would will should not no yes " +
    "user user's has have had like likes really very just also than then there their they them he she his her " +
    "our we us from into over under after before called named always never every each").split(" "));
  const B = {
    items: [], nodes: [], topics: [], pulses: [], flash: new Map(), hover: null, selected: null,
    filter: "", geo: null, size: [0, 0], t: 0, on: false, color: [70, 180, 230],
  };

  // --- deterministic randomness (shapes stay the same between frames) -----------------------
  function rng(seed) {
    return () => {
      seed |= 0; seed = (seed + 0x6D2B79F5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // --- topics ---------------------------------------------------------------------------------
  function words(text) {
    return (text.toLowerCase().replace(/'s\b/g, "").match(/[a-z][a-z']{2,}/g) || [])
      .filter((w) => !STOP.has(w));
  }
  function topics(items) {
    const freq = new Map();
    const kw = items.map((m) => [...new Set(words(m.text))]);
    kw.forEach((ws) => ws.forEach((w) => freq.set(w, (freq.get(w) || 0) + 1)));
    const groups = new Map();
    items.forEach((m, i) => {
      let best = null;
      for (const w of kw[i]) {
        const f = freq.get(w);
        if (f >= 2 && (!best || f > freq.get(best) || (f === freq.get(best) && w.length > best.length))) best = w;
      }
      const key = best || (items.length > 8 ? "other" : (kw[i][0] || "other"));
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push({ m, words: kw[i] });
    });
    return [...groups.entries()].sort((a, b) => b[1].length - a[1].length);
  }

  // --- layout ---------------------------------------------------------------------------------
  function layout() {
    const cv = $("brain");
    const w = cv.clientWidth, h = cv.clientHeight;
    B.size = [w, h];
    const cx = w / 2, cy = h * 0.46;
    const R = Math.max(34, Math.min(w, h) * 0.15);
    B.geo = brainGeometry(cx, cy, R);
    const groups = topics(B.items);
    const total = Math.max(1, B.items.length);
    const minShare = 0.35;
    const weights = groups.map(([, g]) => g.length + minShare);
    const wsum = weights.reduce((a, b) => a + b, 0) || 1;
    const rMin = R * 1.75, rMax = Math.max(rMin + 20, Math.min(w, h) / 2 - 26);
    B.wide = w >= 700;                          // room for labels beside the nodes
    B.kx = B.wide ? Math.min(1.9, Math.max(1, (w / 2 - 190) / rMax)) : 1;   // stretch sideways
    let a0 = -Math.PI / 2;
    B.nodes = [];
    B.topics = [];
    groups.forEach(([name, g], gi) => {
      const span = (weights[gi] / wsum) * Math.PI * 2;
      const pad = Math.min(0.12, span * 0.12);
      g.forEach((entry, i) => {
        const frac = g.length === 1 ? 0.5 : i / (g.length - 1);
        const ang = a0 + pad + frac * (span - 2 * pad);
        const r = rMin + ((i * 37 + gi * 11) % 100) / 100 * (rMax - rMin) * (total > 6 ? 1 : 0.55);
        B.nodes.push({ id: entry.m.id, m: entry.m, words: entry.words, topic: name, ang, r, cx, cy,
          phase: (entry.m.id * 1.7) % 6.28 });
      });
      const mid = a0 + span / 2;
      B.topics.push({ name, ang: mid, r: R * 1.38, count: g.length });
      a0 += span;
    });
  }

  function nodePos(n) {
    const wob = reduced ? 0 : Math.sin(B.t * 0.8 + n.phase) * 3;
    return [n.cx + Math.cos(n.ang) * (n.r + wob) * B.kx, n.cy + Math.sin(n.ang) * (n.r + wob)];
  }

  // --- the brain (top view: two hemispheres, folds, a centre fissure) ---------------------------
  function brainGeometry(cx, cy, R) {
    const rand = rng(7);
    const hemis = [-1, 1].map((side) => {
      const hx = cx + side * R * 0.5, hy = cy + R * 0.02;
      const rx = R * 0.52, ry = R * 0.9;
      const outline = [];
      for (let i = 0; i <= 120; i++) {
        const a = (i / 120) * Math.PI * 2;
        const s = Math.sin(a), c = Math.cos(a);
        const bump = 1 + 0.03 * Math.sin(a * 13 + side) + 0.018 * Math.sin(a * 29 + 2 * side);
        // narrower at the front (top), fuller at the back; the inner edge runs straight
        // down the middle so the halves meet along the fissure
        const taper = s < 0 ? 1 - 0.16 * -s : 1 + 0.04 * s;
        const inner = c * side < 0 ? 0.98 : 1;
        let x = hx + c * rx * bump * taper * inner;
        if ((x - cx) * side < R * 0.02) x = cx + side * R * 0.02;
        outline.push([x, hy + s * ry * bump]);
      }
      const folds = [];
      for (let k = 0; k < 24; k++) {
        let x = hx + (rand() - 0.5) * rx * 1.5, y = hy + (rand() - 0.5) * ry * 1.8;
        let dir = rand() * Math.PI * 2;
        const pts = [[x, y]];
        for (let s = 0; s < 6; s++) {
          dir += (rand() - 0.5) * 2.2;
          x += Math.cos(dir) * R * 0.09;
          y += Math.sin(dir) * R * 0.09;
          pts.push([x, y]);
        }
        folds.push(pts);
      }
      return { outline, folds };
    });
    const neurons = Array.from({ length: 34 }, () => {
      const side = rand() < 0.5 ? -1 : 1;
      const a = rand() * Math.PI * 2, d = Math.sqrt(rand()) * 0.8;
      return [cx + side * R * 0.47 + Math.cos(a) * R * 0.45 * d, cy + Math.sin(a) * R * 0.8 * d, rand() * 6.28];
    });
    return { cx, cy, R, hemis, neurons };
  }

  function drawBrain(g, rgba) {
    const { cx, cy, R, hemis, neurons } = B.geo;
    const halo = g.createRadialGradient(cx, cy, R * 0.3, cx, cy, R * 2.2);
    halo.addColorStop(0, rgba(0.22));
    halo.addColorStop(1, rgba(0));
    g.fillStyle = halo;
    g.fillRect(0, 0, B.size[0], B.size[1]);
    const pulse = reduced ? 0 : 0.5 + 0.5 * Math.sin(B.t * (S.state === "thinking" ? 5 : 1.6));
    for (const h of hemis) {
      g.save();
      g.beginPath();
      h.outline.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y)));
      g.closePath();
      const fill = g.createRadialGradient(cx, cy - R * 0.3, R * 0.1, cx, cy, R * 1.1);
      fill.addColorStop(0, rgba(0.38 + 0.12 * pulse));
      fill.addColorStop(1, rgba(0.1));
      g.fillStyle = fill;
      g.shadowColor = rgba(0.9);
      g.shadowBlur = 18 + 10 * pulse;
      g.fill();
      g.shadowBlur = 0;
      g.lineWidth = 1.6;
      g.strokeStyle = rgba(0.85);
      g.stroke();
      g.clip();
      g.lineWidth = 1.3;
      g.strokeStyle = rgba(0.45 + 0.2 * pulse);
      for (const f of h.folds) {
        g.beginPath();
        g.moveTo(f[0][0], f[0][1]);
        for (let i = 1; i < f.length - 1; i++) {
          const mx = (f[i][0] + f[i + 1][0]) / 2, my = (f[i][1] + f[i + 1][1]) / 2;
          g.quadraticCurveTo(f[i][0], f[i][1], mx, my);
        }
        g.stroke();
      }
      g.restore();
    }
    for (const [x, y, ph] of neurons) {
      const a = reduced ? 0.5 : 0.25 + 0.75 * Math.max(0, Math.sin(B.t * 2.3 + ph));
      g.fillStyle = `rgba(255,255,255,${0.55 * a})`;
      g.beginPath();
      g.arc(x, y, 1.4, 0, Math.PI * 2);
      g.fill();
    }
  }

  // --- frame ----------------------------------------------------------------------------------
  function matches(n) {
    return !B.filter || n.m.text.toLowerCase().includes(B.filter);
  }

  function edgePoint(n) {
    const { cx, cy, R } = B.geo;
    return [cx + Math.cos(n.ang) * R * 1.02, cy + Math.sin(n.ang) * R * 0.92];
  }

  function draw() {
    if (!B.on) return;
    const cv = $("brain");
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(cv.clientWidth * dpr) || cv.height !== Math.round(cv.clientHeight * dpr)) {
      cv.width = Math.round(cv.clientWidth * dpr);
      cv.height = Math.round(cv.clientHeight * dpr);
      layout();
    }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, B.size[0], B.size[1]);
    B.t += 0.016;
    const target = COLORS[S.state] || COLORS.idle;
    B.color = B.color.map((v, i) => v + (target[i] - v) * 0.06);
    const [r, gr, b] = B.color.map(Math.round);
    const rgba = (a) => `rgba(${r},${gr},${b},${a})`;

    // links between memories that share a word (faint), then synapses to the brain
    g.lineWidth = 1;
    for (let i = 0; i < B.nodes.length; i++) {
      for (let j = i + 1; j < B.nodes.length && B.nodes.length < 120; j++) {
        const a = B.nodes[i], c = B.nodes[j];
        if (a.topic === c.topic || !a.words.some((w) => c.words.includes(w))) continue;
        const [x1, y1] = nodePos(a), [x2, y2] = nodePos(c);
        g.strokeStyle = "rgba(167,139,250,0.10)";
        g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
      }
    }
    for (const n of B.nodes) {
      const [x, y] = nodePos(n), [ex, ey] = edgePoint(n);
      const on = matches(n);
      g.strokeStyle = rgba(on ? (n === B.hover || n === B.selected ? 0.6 : 0.18) : 0.05);
      g.lineWidth = n === B.selected ? 1.6 : 1;
      g.beginPath();
      g.moveTo(ex, ey);
      g.quadraticCurveTo((ex + x) / 2 + Math.sin(n.ang) * 18, (ey + y) / 2 - Math.cos(n.ang) * 18, x, y);
      g.stroke();
    }

    drawBrain(g, rgba);

    // pulses: brain -> memory when a memory helps answer
    B.pulses = B.pulses.filter((p) => (p.t += 0.018) < 1);
    for (const p of B.pulses) {
      const n = B.nodes.find((k) => k.id === p.id);
      if (!n) continue;
      const [x, y] = nodePos(n), [ex, ey] = edgePoint(n);
      const qx = (ex + x) / 2 + Math.sin(n.ang) * 18, qy = (ey + y) / 2 - Math.cos(n.ang) * 18;
      const t = p.t, u = 1 - t;
      const px = u * u * ex + 2 * u * t * qx + t * t * x, py = u * u * ey + 2 * u * t * qy + t * t * y;
      g.fillStyle = "rgba(255,255,255,0.95)";
      g.shadowColor = rgba(1); g.shadowBlur = 14;
      g.beginPath(); g.arc(px, py, 3, 0, Math.PI * 2); g.fill();
      g.shadowBlur = 0;
    }

    // memory nodes
    const labels = B.wide && B.nodes.length <= 30;
    g.font = "12px 'Segoe UI', system-ui, sans-serif";
    g.textBaseline = "middle";
    const placed = [];                         // label boxes drawn so far: skip overlapping ones
    const ordered = [...B.nodes].sort((a, c) => (c === B.hover || c === B.selected) - (a === B.hover || a === B.selected));
    for (const n of ordered) {
      const [x, y] = nodePos(n);
      const on = matches(n);
      const fl = B.flash.get(n.id) || 0;
      const big = n === B.hover || n === B.selected || fl > 0;
      g.fillStyle = on ? (fl > 0 ? "#ffffff" : rgba(0.95)) : "rgba(120,130,150,0.25)";
      g.shadowColor = rgba(on ? 0.9 : 0);
      g.shadowBlur = on ? (big ? 18 : 8) : 0;
      g.beginPath(); g.arc(x, y, big ? 7 : 5, 0, Math.PI * 2); g.fill();
      g.shadowBlur = 0;
      if ((labels || n === B.selected || (B.filter && on && B.wide)) && on) {
        const txt = n.m.text.length > 30 ? n.m.text.slice(0, 29) + "…" : n.m.text;
        const right = Math.cos(n.ang) >= 0;
        const tw = g.measureText(txt).width;
        const bx = right ? x + 10 : x - 10 - tw;
        const box = [bx - 2, y - 8, bx + tw + 2, y + 8];
        const clash = placed.some((p) => box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1]);
        if (!clash || n === B.hover || n === B.selected) {
          placed.push(box);
          g.textAlign = "left";
          g.fillStyle = big ? "rgba(235,242,250,0.95)" : "rgba(200,212,228,0.7)";
          g.fillText(txt, bx, y);
        }
      }
    }
    for (const [id, v] of B.flash) {
      if (v <= 0.02) B.flash.delete(id); else B.flash.set(id, v - 0.015);
    }
    // topic names on the outside
    g.font = "600 11px 'Segoe UI', system-ui, sans-serif";
    g.textAlign = "center";
    for (const tp of B.topics) {
      if (tp.name === "other") continue;
      const x = B.geo.cx + Math.cos(tp.ang) * tp.r * 1.15, y = B.geo.cy + Math.sin(tp.ang) * tp.r;
      if (x < 20 || x > B.size[0] - 20 || y < 8 || y > B.size[1] - 8) continue;
      g.fillStyle = "rgba(140,160,190,0.75)";
      g.fillText(tp.name.toUpperCase(), x, y);
    }
    requestAnimationFrame(draw);
  }

  // --- interaction ----------------------------------------------------------------------------
  function hit(ev) {
    const rect = $("brain").getBoundingClientRect();
    const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
    let best = null, bd = 16 * 16;
    for (const n of B.nodes) {
      const [x, y] = nodePos(n);
      const d = (x - mx) ** 2 + (y - my) ** 2;
      if (d < bd) { bd = d; best = n; }
    }
    return [best, mx, my];
  }
  function showTip(n, x, y) {
    const tip = $("brainTip");
    if (!n) { tip.hidden = true; return; }
    tip.textContent = n.m.text;
    tip.hidden = false;
    const w = $("brain").clientWidth;
    tip.style.left = Math.min(Math.max(8, x + 12), w - 270) + "px";
    tip.style.top = Math.max(8, y - 40) + "px";
  }
  function select(n) {
    B.selected = n;
    $("brainCard").hidden = !n;
    if (!n) return;
    $("brainCardText").textContent = n.m.text;
    const when = new Date(n.m.created * 1000);
    $("brainCardWhen").textContent = `Remembered ${when.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })}` +
      (n.topic !== "other" ? ` · topic: ${n.topic}` : "");
  }

  function stats() {
    const n = B.items.length;
    const t = new Set(B.nodes.map((k) => k.topic)).size;
    $("brainStats").textContent = n ? `${n} memor${n === 1 ? "y" : "ies"} · ${t} topic${t === 1 ? "" : "s"}` : "";
    $("brainEmpty").hidden = n > 0;
  }

  function init() {
    const cv = $("brain");
    cv.addEventListener("mousemove", (ev) => {
      const [n, x, y] = hit(ev);
      B.hover = n;
      cv.style.cursor = n ? "pointer" : "default";
      showTip(n && n !== B.selected ? n : null, x, y);
    });
    cv.addEventListener("mouseleave", () => { B.hover = null; showTip(null); });
    cv.addEventListener("click", (ev) => { const [n] = hit(ev); select(n); showTip(null); });
    $("brainClose").onclick = () => select(null);
    $("brainForget").onclick = () => {
      if (!B.selected) return;
      if (confirm(`Forget "${B.selected.m.text}"?`)) send({ type: "mem_delete", id: B.selected.id });
      select(null);
    };
    $("brainSearch").addEventListener("input", (e) => { B.filter = e.target.value.trim().toLowerCase(); });
    $("teach").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = $("teachInput").value.trim();
      if (!v) return;
      send({ type: "mem_add", text: v });
      $("teachInput").value = "";
    });
    window.addEventListener("resize", () => { if (B.on) layout(); });
  }

  return {
    init,
    show(on) {
      B.on = on;
      if (on) { requestAnimationFrame(() => { layout(); stats(); requestAnimationFrame(draw); }); }
    },
    memories(items) {
      const known = new Set(B.items.map((m) => m.id));
      B.items = items || [];
      for (const m of B.items) if (known.size && !known.has(m.id)) B.flash.set(m.id, 1);   // new: flash
      if (B.selected && !B.items.some((m) => m.id === B.selected.id)) select(null);
      if (B.on) { layout(); stats(); }
    },
    used(ids) {
      for (const id of ids || []) { B.pulses.push({ id, t: 0 }); B.flash.set(id, 1.2); }
    },
    count() { return B.items.length; },
  };
})();

document.addEventListener("DOMContentLoaded", () => NovaBrain.init());
