// Nova on the phone. Like the HUD, text is only ever set with textContent (never innerHTML).
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
};

const P = { ws: null, name: "Nova", user: "sir", live: null, chips: new Map(), retry: 0, confirmId: null,
  standby: false, busy: false, speak: false, timers: [], watches: [], skew: 0,
  canHear: false, secureUrl: null, voiceNote: "", rec: null, chunks: [], recTimer: null, player: null, lastVoice: false,
  home: "offline", sending: false, talking: false, meter: null, recStart: 0 };
try { P.speak = localStorage.getItem("novaSpeak") === "1"; } catch (e) { /* ignore */ }

const COLORS = {
  idle: [70, 180, 230], listening: [74, 222, 128], thinking: [167, 139, 250], speaking: [90, 216, 255],
  confirm: [255, 181, 71], standby: [100, 116, 139], muted: [248, 113, 113], offline: [70, 80, 100],
  dictation: [236, 240, 245], recording: [255, 77, 109], enrolling: [52, 211, 153],
};
const AT_HOME = {
  idle: "At home · ready", listening: "At home · hearing someone", thinking: "At home · working",
  speaking: "At home · talking", confirm: "At home · waiting for a yes/no", standby: "Standing down",
  muted: "At home · microphone off", dictation: "At home · dictating", recording: "At home · watching a lesson",
  enrolling: "At home · learning a voice",
};

// --- sign in ---------------------------------------------------------------------------------
async function start() {
  let r;
  try { r = await fetch("/api/me", { credentials: "same-origin" }); } catch (e) { return retryLater(); }
  if (r.ok) { showApp(); connect(); } else showLogin();
}
function retryLater() { banner("Can't reach Nova. Is the PC on and Tailscale connected?"); setTimeout(start, 4000); }
function showLogin(msg) {
  $("app").hidden = true;
  $("login").hidden = false;
  $("loginError").textContent = msg || "";
  $("password").focus();
}
function showApp() { $("login").hidden = true; $("app").hidden = false; greet(); }

async function login(e) {
  e.preventDefault();
  $("loginBtn").disabled = true;
  $("loginError").textContent = "";
  try {
    const r = await fetch("/api/login", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("password").value, code: $("code").value }) });
    const body = await r.json().catch(() => ({}));
    if (r.ok) { $("password").value = ""; $("code").value = ""; showApp(); connect(); }
    else { $("loginError").textContent = body.error || "Couldn't sign in."; $("code").value = ""; $("code").focus(); }
  } catch (err) {
    $("loginError").textContent = "Can't reach Nova. Is Tailscale connected on this phone?";
  } finally { $("loginBtn").disabled = false; }
}
async function logout() {
  await fetch("/api/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
  if (P.ws) { P.ws.onclose = null; P.ws.close(); }
  showLogin("Signed out.");
}

// --- connection ------------------------------------------------------------------------------
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  P.ws = ws;
  ws.onmessage = (m) => { let ev; try { ev = JSON.parse(m.data); } catch (e) { return; } handle(ev, false); };
  ws.onopen = () => { P.retry = 0; $("banner").hidden = true; };
  ws.onclose = (e) => {
    setState("offline");
    if (e.code === 4401) { showLogin("You've been signed out. Sign in again."); return; }
    banner("Reconnecting to Nova…");
    P.retry = Math.min(P.retry + 1, 6);
    setTimeout(connect, 800 * P.retry);
  };
}
function send(msg) { if (P.ws && P.ws.readyState === 1) { P.ws.send(JSON.stringify(msg)); return true; } return false; }
let bannerTimer = null;
function banner(text, ok) {
  const b = $("banner");
  b.textContent = text; b.className = "banner" + (ok ? " ok" : ""); b.hidden = false;
  clearTimeout(bannerTimer);
  if (ok) bannerTimer = setTimeout(() => { b.hidden = true; }, 3000);
}

// --- events ----------------------------------------------------------------------------------
function handle(ev, replay) {
  switch (ev.type) {
    case "hello":
      P.name = ev.name || "Nova"; P.user = ev.user || "";
      $("name").textContent = P.name.toUpperCase(); $("confirmWho").textContent = P.name;
      $("deviceName").textContent = "This device: " + (ev.device || "phone");
      $("askInput").placeholder = `Message ${P.name}…`;
      P.canHear = !!ev.voice; P.secureUrl = ev.secure_url || null; P.voiceNote = ev.voice_note || "";
      $("chat").replaceChildren(); P.live = null; P.chips.clear();
      for (const past of ev.backlog || []) handle(past, true);
      greet(); empty();
      return;
    case "status": return status(ev);
    case "timers": P.timers = ev.items || []; P.watches = ev.watches || [];
      P.skew = (ev.now || Date.now() / 1000) - Date.now() / 1000; renderNext(); return;
    case "vitals": return vitals(ev);
    case "you": closeLive(); addMsg(ev.voice ? "you voice" : "you", ev.text); setBusy(true);
      if (!replay) P.lastVoice = !!ev.voice; break;
    case "audio": if (!replay) playVoice(ev.data, ev.mime); break;
    case "text":
      if (!P.live) P.live = addMsg("nova live", "");
      P.live.textContent += ev.text;
      scroll();
      break;
    case "tool_started": chip(ev.name); break;
    case "tool_finished": chipDone(ev.name, ev.is_error, ev.duration_ms); break;
    case "turn_complete": {
      const said = P.live ? P.live.textContent : "";
      closeLive(); setBusy(false);
      if (!replay && P.speak && said && !P.lastVoice) speakOut(said);   // spoken requests get Nova's own voice
      break;
    }
    case "cancelled": closeLive(); setBusy(false); addSys("sys", "Stopped"); break;
    case "error": closeLive(); setBusy(false); addSys("sys err", ev.message || "Something went wrong"); break;
    case "announcement":
      addSys("sys bell", "⏰ " + ev.text);
      if (!replay && navigator.vibrate) navigator.vibrate([80, 60, 80]);
      if (!replay && P.speak) speakOut(ev.text);
      break;
    case "sys": addSys("sys", ev.text); break;
    case "confirm_request": if (!replay) showConfirm(ev.id, ev.text || ev.tool); break;
  }
  empty();
}

function pretty(name) {
  const map = { open_app: "Open app", system_status: "PC status", web_search: "Web search", escalate: "Claude",
    look_at_screen: "Screen", set_timer: "Timer", set_reminder: "Reminder", now_playing: "Now playing" };
  const s = map[name] || String(name || "").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
function addMsg(cls, text) { const li = el("li", "msg " + cls, text); $("chat").append(li); trim(); scroll(); return li; }
function addSys(cls, text) { const li = el("li", cls, text); $("chat").append(li); trim(); scroll(); return li; }
function closeLive() {
  if (!P.live) return;
  P.live.classList.remove("live");
  if (!P.live.textContent.trim()) P.live.remove();
  P.live = null;
}
function chip(name) {
  let row = $("chat").lastElementChild;
  if (!row || !row.classList.contains("chips")) { row = el("li", "chips"); $("chat").append(row); }
  const c = el("span", "chip");
  c.append(el("i"), document.createTextNode(pretty(name)));
  row.append(c);
  P.chips.set(name, c);
  scroll();
}
function chipDone(name, bad, ms) {
  let c = P.chips.get(name);
  if (!c) { chip(name); c = P.chips.get(name); }
  P.chips.delete(name);
  c.classList.add(bad ? "bad" : "ok");
  if (ms != null) c.append(document.createTextNode(` ${(ms / 1000).toFixed(1)} s`));
}
function trim() { const c = $("chat"); while (c.children.length > 200) c.firstChild.remove(); }
function scroll() { const s = $("chatScroll"); requestAnimationFrame(() => { s.scrollTop = s.scrollHeight; }); }
function empty() {
  const none = $("chat").children.length === 0;
  $("empty").hidden = !none;
  $("stage").classList.toggle("big", none);
}
function greet() {
  const h = new Date().getHours();
  $("greeting").textContent = `Good ${h < 5 ? "evening" : h < 12 ? "morning" : h < 18 ? "afternoon" : "evening"}${P.user ? ", " + P.user : ""}.`;
}
function setBusy(on) { P.busy = on; $("stopBtn").hidden = !on; }

// --- status ----------------------------------------------------------------------------------
function status(ev) {
  P.standby = !!ev.standby;
  setState(ev.state);
  $("standLabel").textContent = P.standby ? "Wake up" : "Stand down";
  $("standBtn").dataset.action = P.standby ? "wake" : "stand_down";
  $("standBtn").classList.toggle("wake", P.standby);
}
function setState(state) {
  P.home = state;
  $("stateText").textContent = state === "offline" ? "Not connected" : AT_HOME[state] || AT_HOME.idle;
  const c = COLORS[state] || COLORS.idle;
  document.documentElement.style.setProperty("--glow", c.join(", "));
  $("dot").classList.toggle("busy", state === "thinking" || state === "speaking");
}

// --- Nova's bubble -----------------------------------------------------------------------------
// What the bubble shows is this conversation: listening while you talk, thinking while Nova
// works on it, speaking while its reply plays. Otherwise it follows Nova at home (standing down,
// mic off, not connected).
function orbState() {
  if (P.home === "offline") return "offline";
  if (P.rec) return "listening";
  if (P.confirmId) return "confirm";
  if (P.talking) return "speaking";
  if (P.busy || P.sending) return "thinking";
  if (P.home === "standby" || P.home === "muted") return P.home;
  return "idle";
}
const HINTS = { listening: "Listening… tap to send", thinking: "Thinking…", speaking: "Speaking · tap to talk",
  confirm: "Waiting for your yes or no", standby: "Standing down · tap to talk", offline: "Not connected" };
let lastHint = "";
function orbFrame() {
  const state = orbState();
  let hint = HINTS[state] || "Tap to talk";
  if (state === "listening") hint = `${fmt((Date.now() - P.recStart) / 1000)} · tap to send`;
  if (hint !== lastHint) {
    lastHint = hint;
    $("orbHint").textContent = hint;
    $("orbHint").classList.toggle("rec", state === "listening");
  }
  return { state, level: P.rec ? micLevel() : 0, color: COLORS[state] || COLORS.idle };
}
function micLevel() {
  const m = P.meter;
  if (!m) return 0.2;
  m.an.getFloatTimeDomainData(m.buf);
  let sum = 0;
  for (const v of m.buf) sum += v * v;
  return Math.min(1, Math.sqrt(sum / m.buf.length) * 8);
}
function startMeter(stream) {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const an = ctx.createAnalyser();
    an.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(an);
    P.meter = { ctx, an, buf: new Float32Array(an.fftSize) };
  } catch (e) { P.meter = null; }
}
function stopMeter() {
  if (P.meter) { P.meter.ctx.close().catch(() => {}); P.meter = null; }
}

function fmt(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
}
function renderNext() {
  const now = Date.now() / 1000 + P.skew;
  const items = [...P.timers].sort((a, b) => a.due - b.due).slice(0, 3);
  const watches = P.watches.slice(0, Math.max(0, 3 - items.length));
  $("nextBox").hidden = items.length + watches.length === 0;
  const list = $("nextList");
  list.replaceChildren();
  for (const t of items) {
    const li = el("li");
    const x = el("button", "x", "✕"); x.type = "button"; x.setAttribute("aria-label", "Cancel");
    x.onclick = () => send({ type: "cancel_timer", id: t.id });
    const right = el("span"); right.append(el("b", "", fmt(t.due - now)), x);
    li.append(el("span", "", t.text || t.kind.charAt(0).toUpperCase() + t.kind.slice(1)), right);
    list.append(li);
  }
  for (const w of watches) {
    const li = el("li", "w");
    li.append(el("span", "", w.text.charAt(0).toUpperCase() + w.text.slice(1)), el("b", "", "watching"));
    list.append(li);
  }
}
function vitals(ev) {
  const show = (k, text, v, warn, hot) => {
    const d = document.querySelector(`.vital[data-k="${k}"]`);
    d.querySelector("b").textContent = text;
    d.classList.toggle("warn", v != null && v >= warn && v < hot);
    d.classList.toggle("hot", v != null && v >= hot);
  };
  show("cpu", ev.cpu == null ? "–" : `${ev.cpu}%`, ev.cpu, 75, 92);
  show("ram", ev.ram == null ? "–" : `${ev.ram}%`, ev.ram, 80, 92);
  show("gpu_temp", ev.gpu_temp == null ? "–" : `${ev.gpu_temp}°C`, ev.gpu_temp, 80, 88);
}

// --- confirmation, speech --------------------------------------------------------------------
function showConfirm(id, text) {
  P.confirmId = id;
  $("confirmText").textContent = text + "?";
  $("confirm").hidden = false;
  if (navigator.vibrate) navigator.vibrate(60);
}
function answer(yes) {
  if (P.confirmId) send({ type: "confirm", id: P.confirmId, approved: yes });
  P.confirmId = null;
  $("confirm").hidden = true;
}
function speakOut(text) {
  if (!("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(text);
  const v = speechSynthesis.getVoices().find((x) => x.lang === "en-GB");
  if (v) u.voice = v;
  speechSynthesis.cancel();
  speechSynthesis.speak(u);
}

// --- talking to Nova ---------------------------------------------------------------------------
// The phone records a voice message; the PC hears it with the same Whisper as the headset,
// answers with the same brain, and sends back Nova's own voice.
const MAX_REC_MS = 60000;
function micHelp() {
  if (P.secureUrl && location.origin !== new URL(P.secureUrl).origin)
    return `Voice works on the secure address: ${P.secureUrl} (open that and sign in once).`;
  return P.voiceNote || "This browser won't let the page use the microphone.";
}
function unlockAudio() {
  // Phones only play sound started by a tap: prime one player now, reuse it for the reply.
  if (!P.player) P.player = new Audio();
  try {
    const sr = 8000, n = 800, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
    const w = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
    w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVEfmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
    P.player.src = URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
    P.player.play().catch(() => {});
  } catch (e) { /* ignore */ }
}
function playVoice(b64, mime) {
  try {
    const bin = atob(b64), bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    if (!P.player) P.player = new Audio();
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    const old = P.player.src;
    P.player.src = URL.createObjectURL(new Blob([bytes], { type: mime || "audio/wav" }));
    if (old && old.startsWith("blob:")) URL.revokeObjectURL(old);
    P.player.onplaying = () => { P.talking = true; };
    P.player.onended = P.player.onpause = () => { P.talking = false; };
    P.player.play().catch(() => { P.talking = false; banner("Tap anywhere to hear Nova's reply.", true); });
  } catch (e) { /* ignore */ }
}
function pickMime() {
  const kinds = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
  if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
  return kinds.find((k) => MediaRecorder.isTypeSupported(k)) || "";
}
async function toggleMic() {
  if (P.rec) { stopRec(); return; }
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
    banner(micHelp()); return;
  }
  if (!P.canHear) { banner("Nova's hearing isn't running on the PC right now."); return; }
  unlockAudio();
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
  catch (e) { banner("Allow the microphone for this page to talk to Nova."); return; }
  const mime = pickMime();
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  P.rec = rec; P.chunks = [];
  rec.ondataavailable = (e) => { if (e.data && e.data.size) P.chunks.push(e.data); };
  rec.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(P.chunks, { type: rec.mimeType || mime || "audio/webm" });
    P.rec = null; P.chunks = [];
    sendVoice(blob);
  };
  rec.start();
  P.recStart = Date.now();
  startMeter(stream);
  $("micBtn").classList.add("rec"); $("micBtn").setAttribute("aria-pressed", "true");
  if (navigator.vibrate) navigator.vibrate(20);
  P.recTimer = setTimeout(stopRec, MAX_REC_MS);
}
function stopRec() {
  clearTimeout(P.recTimer);
  stopMeter();
  $("micBtn").classList.remove("rec"); $("micBtn").setAttribute("aria-pressed", "false");
  if (P.rec && P.rec.state !== "inactive") P.rec.stop();
}
async function sendVoice(blob) {
  if (!blob.size) return;
  P.sending = true;
  $("micBtn").classList.add("sending");
  try {
    const r = await fetch("/api/voice", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": blob.type || "application/octet-stream" }, body: blob });
    const body = await r.json().catch(() => ({}));
    if (r.status === 401) { showLogin("Sign in again."); return; }
    if (!r.ok) addSys("sys err", body.error || "Couldn't send that.");
  } catch (e) {
    addSys("sys err", "Couldn't reach Nova. Is Tailscale connected?");
  } finally { P.sending = false; $("micBtn").classList.remove("sending"); }
}

// --- wiring ----------------------------------------------------------------------------------
function init() {
  $("loginForm").addEventListener("submit", login);
  $("ask").addEventListener("submit", (e) => {
    e.preventDefault();
    const v = $("askInput").value.trim();
    if (!v) return;
    if (!send({ type: "text", text: v })) { banner("Not connected yet. Try again in a moment."); return; }
    $("askInput").value = "";
  });
  for (const b of document.querySelectorAll(".suggest button")) b.onclick = () => send({ type: "text", text: b.dataset.say });
  for (const b of document.querySelectorAll(".actions button")) {
    b.onclick = () => {
      if (!send({ type: "action", name: b.dataset.action })) return;
      b.classList.add("busy");
      setTimeout(() => b.classList.remove("busy"), 600);
      if (navigator.vibrate) navigator.vibrate(15);
    };
  }
  $("stopBtn").onclick = () => { send({ type: "cancel" }); if (P.player) P.player.pause(); };
  $("micBtn").onclick = toggleMic;
  $("orbBtn").onclick = toggleMic;
  $("askInput").addEventListener("focus", () => document.body.classList.add("typing"));
  $("askInput").addEventListener("blur", () => document.body.classList.remove("typing"));
  if (typeof NovaOrb !== "undefined") { NovaOrb.add($("orb")); NovaOrb.start(orbFrame); }
  $("yesBtn").onclick = () => answer(true);
  $("noBtn").onclick = () => answer(false);
  $("menuBtn").onclick = () => {
    const open = $("menu").hidden;
    $("menu").hidden = !open;
    $("menuBtn").setAttribute("aria-expanded", String(open));
  };
  document.addEventListener("click", (e) => {
    if (!$("menu").hidden && !$("menu").contains(e.target) && !$("menuBtn").contains(e.target)) $("menu").hidden = true;
  });
  $("speakToggle").checked = P.speak;
  $("speakToggle").onchange = (e) => {
    P.speak = e.target.checked;
    try { localStorage.setItem("novaSpeak", P.speak ? "1" : "0"); } catch (err) { /* ignore */ }
    if (P.speak) speakOut("Very good, sir.");
  };
  $("logoutBtn").onclick = logout;
  setInterval(renderNext, 1000);
  start();
}
document.addEventListener("DOMContentLoaded", init);
