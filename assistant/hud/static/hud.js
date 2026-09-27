// Nova HUD. Everything shown comes from speech and tools, so text is only ever set with
// textContent (never innerHTML): nothing said out loud can inject code into this page.
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

// --- key: comes in the URL fragment (never sent to the server over HTTP) ---------------------
let key = "";
try {
  const m = location.hash.match(/k=([A-Za-z0-9_-]+)/);
  if (m) { key = m[1]; sessionStorage.setItem("novaKey", key); history.replaceState(null, "", "/"); }
  else key = sessionStorage.getItem("novaKey") || "";
} catch (e) { /* storage blocked: the fragment still works for this load */ }

const S = {
  ws: null, name: "Nova", user: "sir", routines: [], confirmTimeout: 12,
  state: "idle", level: 0, mode: "wake", standby: false, muted: false,
  live: null, confirmShown: null, confirmTimer: null, unseenAct: 0, tab: "chat",
  timers: [], clockSkew: 0, showIgnored: false, retry: 0,
};

// --- connection ------------------------------------------------------------------------------
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => { ws.send(JSON.stringify({ type: "auth", token: key })); };
  ws.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (ev.type === "hello") { S.retry = 0; setConn(true); hello(ev); return; }
    handle(ev, false);
  };
  ws.onclose = (e) => {
    setConn(false);
    if (e.code === 4401) {
      showOffline("This window's key is out of date (Nova was restarted).",
        "Close it and run .venv\\Scripts\\python -m assistant hud");
      return;
    }
    showOffline("Nova isn't running.", "Start it with .venv\\Scripts\\python -m assistant voice. This window reconnects by itself.");
    S.retry = Math.min(S.retry + 1, 5);
    setTimeout(connect, 600 * S.retry);
  };
}
function send(msg) { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(msg)); }
function setConn(on) {
  $("conn").className = "conn " + (on ? "on" : "off");
  $("conn").title = on ? "Connected" : "Not connected";
  if (on) $("offline").hidden = true;
  if (!on) { S.state = "offline"; renderState(); }
}
function showOffline(title, small) {
  $("offline").hidden = false;
  $("offlineText").textContent = title;
  $("offline").querySelector(".small").textContent = small;
}

function hello(ev) {
  S.name = ev.name || "Nova";
  S.user = ev.user || "";
  S.confirmTimeout = ev.confirm_timeout_s || 12;
  S.routines = ev.routines || [];
  S.showIgnored = !!ev.show_ignored;
  $("showIgnored").checked = S.showIgnored;
  $("name").textContent = S.name.toUpperCase();
  $("confirmWho").textContent = S.name;
  document.title = S.name;
  $("askInput").placeholder = `Type to ${S.name}…`;
  $("chat").replaceChildren();
  $("activity").replaceChildren();
  S.live = null;
  S.unseenAct = 0;
  for (const past of ev.backlog || []) handle(past, true);
  S.unseenAct = 0;
  badge();
  renderRoutines();
  empties();
}

// --- events ----------------------------------------------------------------------------------
function handle(ev, replay) {
  switch (ev.type) {
    case "status": return status(ev);
    case "timers": return timersIn(ev);
    case "memories": return NovaBrain.memories(ev.items);
    case "memory_used": if (!replay) NovaBrain.used(ev.ids); return;
    case "toast": return toast(ev.text);
    case "transcript":
      closeLive();
      addMsg("you" + (ev.typed ? " typed" : ""), ev.text);
      break;
    case "text":
      if (!S.live) { S.live = addMsg("nova live", ""); }
      S.live.textContent += ev.text;
      scrollChat();
      break;
    case "speak":
      if (ev.fixed && ev.show !== false) { closeLive(); addMsg("nova", ev.text); }
      break;
    case "turn_complete": case "latency": case "idle":
      closeLive();
      break;
    case "interrupted": case "stopped":
      if (S.live) { S.live.classList.add("cut"); closeLive(); }
      else if (ev.type === "stopped") addSys("sys", "Stopped");
      break;
    case "tool":
      addSys("sys tool", `Using ${pretty(ev.name, false)}…`);
      activity("run", pretty(ev.name), "", ev.ts);
      break;
    case "tool_done": {
      const item = lastRunning(ev.name);
      const detail = (ev.summary || "") + (ev.ms != null ? `  ·  ${(ev.ms / 1000).toFixed(1)} s` : "");
      if (item) finishActivity(item, ev.is_error ? "bad" : "ok", detail);
      else activity(ev.is_error ? "bad" : "ok", pretty(ev.name), detail, ev.ts);
      if (ev.is_error) addSys("sys tool err", `${pretty(ev.name)} didn't work`);
      break;
    }
    case "announcement":
      addSys("sys bell", "⏰ " + ev.text);
      activity("ok", "Reminder", ev.text, ev.ts);
      break;
    case "confirm_request":
      addSys("sys warn", `Asked: ${ev.text}?`);
      if (!replay) showConfirm(ev.text, true);
      break;
    case "confirm_result":
      addSys(ev.approved ? "sys" : "sys warn", ev.approved ? "You said yes" : "Cancelled");
      hideConfirm();
      break;
    case "standby":
      addSys("sys warn", ev.on ? "Standing down" : "Back on duty");
      break;
    case "dictation":
      addSys("sys warn", ev.on ? "✎ Dictation on: everything you say is typed" : "✎ Dictation off");
      break;
    case "dictated":
      addSys("sys dict", "✎ " + ev.text);
      break;
    case "merged":
      addSys("sys", "You kept talking: one request");
      break;
    case "error":
      addSys("sys err", ev.message || "Something went wrong");
      activity("bad", "Error", ev.message || "", ev.ts);
      break;
    case "ignored":
      activity("ignored", `Heard “${ev.text}”`, ev.reason || "", ev.ts);
      break;
  }
  if (!replay) empties();
}

function pretty(name, cap = true) {
  const map = { open_app: "open app", music_control: "music", play_music: "Spotify", now_playing: "now playing",
    web_search: "web search", escalate: "Claude", look_at_screen: "screen", run_routine: "routine",
    system_status: "PC status", open_website: "browser", find_files: "file search" };
  const s = map[name] || String(name || "").replace(/_/g, " ");
  return cap ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// --- chat ------------------------------------------------------------------------------------
function addMsg(cls, text) {
  const li = el("li", "msg " + cls, text);
  li.title = new Date().toLocaleTimeString();
  $("chat").append(li);
  trim($("chat"), 300);
  scrollChat();
  return li;
}
function addSys(cls, text) { const li = el("li", cls, text); $("chat").append(li); scrollChat(); return li; }
function closeLive() {
  if (!S.live) return;
  S.live.classList.remove("live");
  if (!S.live.textContent.trim() && !S.live.classList.contains("cut")) S.live.remove();
  S.live = null;
}
function scrollChat() {
  const p = $("tab-chat");
  if (p.scrollHeight - p.scrollTop - p.clientHeight < 160) requestAnimationFrame(() => { p.scrollTop = p.scrollHeight; });
}
function trim(list, max) { while (list.children.length > max) list.firstChild.remove(); }

// --- activity --------------------------------------------------------------------------------
function activity(kind, what, detail, ts) {
  if (kind === "ignored" && !S.showIgnored) return null;
  const li = el("li", "act" + (kind === "ignored" ? " ignored" : ""));
  li.dataset.tool = what;
  li.dataset.kind = kind;
  const icon = { run: "⟳", ok: "✓", bad: "✗", ignored: "·" }[kind];
  li.append(el("span", "ic " + kind, icon));
  const mid = el("div");
  mid.append(el("div", "what", what));
  const d = el("div", "detail", detail);
  mid.append(d);
  li.append(mid);
  const when = ts ? new Date(ts * 1000) : new Date();
  li.append(el("span", "when", when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })));
  $("activity").prepend(li);
  while ($("activity").children.length > 200) $("activity").lastChild.remove();
  if (S.tab !== "activity" && kind !== "ignored") { S.unseenAct++; badge(); }
  return li;
}
function lastRunning(name) {
  for (const li of $("activity").children) {
    if (li.dataset.kind === "run" && li.dataset.tool === pretty(name)) return li;
  }
  return null;
}
function finishActivity(li, kind, detail) {
  li.dataset.kind = kind;
  const ic = li.querySelector(".ic");
  ic.className = "ic " + kind;
  ic.textContent = kind === "ok" ? "✓" : "✗";
  li.querySelector(".detail").textContent = detail;
}
function badge() {
  $("actBadge").hidden = S.unseenAct === 0;
  $("actBadge").textContent = S.unseenAct > 99 ? "99+" : String(S.unseenAct);
}

// --- status + orb ----------------------------------------------------------------------------
const LABEL = {
  idle: () => S.mode === "open_mic" ? "Listening to everything" : `Say “${S.name}, …”`,
  listening: () => "Listening…", thinking: () => "Thinking…", speaking: () => "Speaking",
  confirm: () => "Waiting for your answer", muted: () => "Microphone off",
  dictation: () => "Dictating: say “stop dictation” when done",
  standby: () => `Standing down. Say “${S.name}, wake up” or press Wake up`,
  offline: () => "Not connected",
};
function status(ev) {
  S.state = ev.state; S.level = ev.level || 0; S.mode = ev.mode;
  S.standby = ev.standby; S.muted = ev.muted;
  if (ev.confirm && !S.confirmShown) showConfirm(ev.confirm, false);
  if (!ev.confirm && S.confirmShown) hideConfirm();
  renderState();
}
function renderState() {
  $("stateText").textContent = (LABEL[S.state] || LABEL.idle)();
  $("muteBtn").setAttribute("aria-pressed", String(!!S.muted));
  $("muteBtn").title = S.muted ? "Microphone is off: click to turn it on" : "Turn the microphone off";
  const stand = $("standBtn");
  stand.textContent = S.standby ? "Wake up" : "Stand down";
  stand.classList.toggle("wake", !!S.standby);
  const open = S.mode === "open_mic";
  $("modeBtn").textContent = open ? "Open mic" : "Wake word";
  $("modeBtn").classList.toggle("open", open);
}

const COLORS = {
  idle: [70, 180, 230], listening: [74, 222, 128], thinking: [167, 139, 250], speaking: [90, 216, 255],
  confirm: [255, 181, 71], standby: [100, 116, 139], muted: [248, 113, 113], offline: [70, 80, 100],
  dictation: [236, 240, 245],
};
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const orb = { c: [70, 180, 230], amp: 0.02, lvl: 0, glow: 0.5, t: 0 };
function drawOrb() {
  const cv = $("orb");
  const dpr = window.devicePixelRatio || 1;
  const size = cv.clientWidth;
  if (cv.width !== size * dpr) { cv.width = size * dpr; cv.height = size * dpr; }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, size, size);

  const st = S.state;
  const target = COLORS[st] || COLORS.idle;
  orb.c = orb.c.map((v, i) => v + (target[i] - v) * 0.08);
  orb.lvl += ((st === "listening" || st === "idle" || st === "dictation" ? S.level : 0) - orb.lvl) * 0.25;
  const speed = reduced ? 0.25 : 1;
  orb.t += 0.016 * speed * (st === "thinking" ? 2.4 : st === "speaking" ? 1.6 : 1);
  const t = orb.t;
  const pulse = st === "speaking" ? 0.5 + 0.5 * Math.sin(t * 7) : 0;
  const wantAmp = { idle: 0.018, listening: 0.03, thinking: 0.05, speaking: 0.03, confirm: 0.03,
    standby: 0.008, muted: 0.01, offline: 0.006 }[st] || 0.02;
  orb.amp += (wantAmp + orb.lvl * 0.2 + pulse * 0.02 - orb.amp) * 0.1;
  const wantGlow = { standby: 0.2, offline: 0.1, muted: 0.3 }[st] ?? 0.55 + orb.lvl * 0.6 + pulse * 0.2;
  orb.glow += (wantGlow - orb.glow) * 0.08;

  const cx = size / 2, cy = size / 2;
  const R = size * 0.27 * (1 + orb.lvl * 0.35 + pulse * 0.04);
  const [r, gr, b] = orb.c.map(Math.round);
  const rgba = (a) => `rgba(${r},${gr},${b},${a})`;

  // outer glow
  const halo = g.createRadialGradient(cx, cy, R * 0.6, cx, cy, R * 1.9);
  halo.addColorStop(0, rgba(0.28 * orb.glow));
  halo.addColorStop(1, rgba(0));
  g.fillStyle = halo;
  g.fillRect(0, 0, size, size);

  // HUD rings
  g.lineCap = "round";
  const ring = (rad, width, a, from, len, alpha) => {
    g.beginPath(); g.strokeStyle = rgba(alpha); g.lineWidth = width;
    g.arc(cx, cy, rad, from, from + len); g.stroke();
  };
  const spin = st === "thinking" ? 2.2 : 0.4;
  for (let i = 0; i < 3; i++) ring(R * 1.42, 1.5, 0, t * spin + i * 2.094, 1.2, 0.35 * orb.glow + 0.08);
  for (let i = 0; i < 4; i++) ring(R * 1.58, 1, 0, -t * spin * 0.6 + i * 1.571, 0.5, 0.2 * orb.glow + 0.05);

  // blob
  g.beginPath();
  const N = 96;
  for (let i = 0; i <= N; i++) {
    const a = (i / N) * Math.PI * 2;
    const wob = Math.sin(3 * a + t * 1.3) * 0.5 + Math.sin(5 * a - t * 0.9) * 0.3 + Math.sin(7 * a + t * 2.1) * 0.2;
    const rad = R * (1 + orb.amp * wob);
    const x = cx + Math.cos(a) * rad, y = cy + Math.sin(a) * rad;
    i ? g.lineTo(x, y) : g.moveTo(x, y);
  }
  const fill = g.createRadialGradient(cx - R * 0.25, cy - R * 0.3, 0, cx, cy, R * 1.05);
  const lift = (v) => Math.round(v + (255 - v) * 0.6);
  fill.addColorStop(0, `rgba(${lift(r)},${lift(gr)},${lift(b)},${0.5 + 0.45 * orb.glow})`);
  fill.addColorStop(0.45, rgba(0.55 + 0.3 * orb.glow));
  fill.addColorStop(1, rgba(0.15));
  g.fillStyle = fill;
  g.shadowColor = rgba(0.8);
  g.shadowBlur = 30 * orb.glow;
  g.fill();
  g.shadowBlur = 0;
  g.strokeStyle = rgba(0.7);
  g.lineWidth = 1.2;
  g.stroke();
  requestAnimationFrame(drawOrb);
}

// --- confirmation ----------------------------------------------------------------------------
function showConfirm(text, fresh) {
  S.confirmShown = text;
  $("confirmText").textContent = text + "?";
  $("confirm").hidden = false;
  const bar = $("confirmBar");
  bar.style.transition = "none";
  bar.style.transform = "scaleX(1)";
  if (fresh && !reduced) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      bar.style.transition = `transform ${S.confirmTimeout}s linear`;
      bar.style.transform = "scaleX(0)";
    }));
  }
  if (document.activeElement !== $("askInput")) $("yesBtn").focus({ preventScroll: true });
}
function hideConfirm() { S.confirmShown = null; $("confirm").hidden = true; }
function answer(yes) { send({ type: "confirm", approved: yes }); hideConfirm(); }

// --- timers ----------------------------------------------------------------------------------
function timersIn(ev) {
  S.timers = ev.items || [];
  S.watches = ev.watches || [];
  S.clockSkew = (ev.now || Date.now() / 1000) - Date.now() / 1000;
  renderTimers();
}
function fmtLeft(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
function renderTimers() {
  const list = $("timers");
  const now = Date.now() / 1000 + S.clockSkew;
  const existing = new Map([...list.children].map((li) => [li.dataset.id, li]));
  const keep = new Set();
  for (const t of S.timers) {
    keep.add(t.id);
    let li = existing.get(t.id);
    if (!li) {
      li = el("li", "timer");
      li.dataset.id = t.id;
      const info = el("div");
      info.append(el("div", "label", t.text || (t.kind === "alarm" ? "Alarm" : t.kind === "timer" ? "Timer" : "Reminder")));
      const at = new Date(t.due * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      info.append(el("div", "kind", `${t.kind} · ${at}`));
      const x = el("button", "x", "✕");
      x.type = "button";
      x.title = "Cancel";
      x.setAttribute("aria-label", `Cancel ${t.kind} ${t.text || ""}`);
      x.onclick = () => send({ type: "cancel_timer", id: t.id });
      li.append(info, el("span", "left"), x);
      list.append(li);
    }
    li.querySelector(".left").textContent = fmtLeft(t.due - now);
  }
  for (const w of S.watches || []) {
    const id = "w:" + w.id;
    keep.add(id);
    let li = existing.get(id);
    if (!li) {
      li = el("li", "timer watching");
      li.dataset.id = id;
      const info = el("div");
      info.append(el("div", "label", w.text.charAt(0).toUpperCase() + w.text.slice(1)), el("div", "kind", "watching for it"));
      const x = el("button", "x", "✕");
      x.type = "button";
      x.title = "Stop watching";
      x.setAttribute("aria-label", `Stop watching for ${w.text}`);
      x.onclick = () => send({ type: "cancel_watch", id: w.id });
      li.append(info, el("span", "left eye", "◉"), x);
      list.append(li);
    }
  }
  for (const [id, li] of existing) if (!keep.has(id)) li.remove();
  const count = S.timers.length + (S.watches || []).length;
  $("timerBadge").hidden = count === 0;
  $("timerBadge").textContent = String(count);
  empties();
}

// --- routines --------------------------------------------------------------------------------
function renderRoutines() {
  const box = $("routines");
  box.replaceChildren();
  for (const r of S.routines) {
    const b = el("button", "routine");
    b.type = "button";
    b.append(el("b", "", r.label), el("span", "", `“${r.phrase}”`));
    b.onclick = () => send({ type: "routine", name: r.name });
    box.append(b);
  }
  if (!S.routines.length) box.append(el("p", "empty", "No routines set up."));
}

function empties() {
  $("chatEmpty").hidden = $("chat").children.length > 0;
  $("actEmpty").hidden = $("activity").children.length > 0;
  $("timersEmpty").hidden = S.timers.length + (S.watches || []).length > 0;
}

let toastTimer = null;
function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

function showBrain(on) {
  document.body.classList.toggle("brain", on);
  $("brainView").hidden = !on;
  $("brainBtn").setAttribute("aria-pressed", String(on));
  NovaBrain.show(on);
  try { sessionStorage.setItem("novaView", on ? "brain" : "hud"); } catch (e) { /* ignore */ }
}

// --- UI wiring -------------------------------------------------------------------------------
function selectTab(name) {
  S.tab = name;
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const p of document.querySelectorAll("main > .panel")) p.hidden = p.id !== "tab-" + name;
  if (name === "activity") { S.unseenAct = 0; badge(); }
  if (name === "chat") scrollChat();
}

function init() {
  for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => selectTab(b.dataset.tab);
  $("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("askInput").value.trim();
    if (!v) return;
    send({ type: "text", text: v });
    $("askInput").value = "";
    selectTab("chat");
  });
  $("yesBtn").onclick = () => answer(true);
  $("noBtn").onclick = () => answer(false);
  $("stopBtn").onclick = () => send({ type: "stop" });
  $("brainBtn").onclick = () => showBrain($("brainView").hidden);
  $("standBtn").onclick = () => send({ type: S.standby ? "resume" : "stand_down" });
  $("muteBtn").onclick = () => send({ type: "mute", on: !S.muted });
  $("modeBtn").onclick = () => send({ type: "mode", mode: S.mode === "open_mic" ? "wake" : "open_mic" });
  $("showIgnored").onchange = (e) => {
    S.showIgnored = e.target.checked;
    if (!S.showIgnored) for (const li of [...$("activity").querySelectorAll(".ignored")]) li.remove();
    empties();
  };
  document.addEventListener("keydown", (e) => {
    const typing = document.activeElement && document.activeElement.tagName === "INPUT";
    if (S.confirmShown && !typing && (e.key === "y" || e.key === "Y")) answer(true);
    else if (S.confirmShown && !typing && (e.key === "n" || e.key === "N")) answer(false);
    else if (e.key === "Escape") send({ type: "stop" });
  });
  setInterval(renderTimers, 1000);
  try { if (sessionStorage.getItem("novaView") === "brain") setTimeout(() => showBrain(true), 0); } catch (e) { /* ignore */ }
  renderState();
  requestAnimationFrame(drawOrb);
  if (!key) {
    showOffline("This window has no key.", "Open it with .venv\\Scripts\\python -m assistant hud");
    return;
  }
  connect();
}

document.addEventListener("DOMContentLoaded", init);
