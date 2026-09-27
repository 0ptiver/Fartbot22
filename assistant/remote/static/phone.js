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
  standby: false, busy: false, speak: false, timers: [], watches: [], skew: 0 };
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
  const ws = new WebSocket(`ws://${location.host}/ws`);
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
      $("chat").replaceChildren(); P.live = null; P.chips.clear();
      for (const past of ev.backlog || []) handle(past, true);
      greet(); empty();
      return;
    case "status": return status(ev);
    case "timers": P.timers = ev.items || []; P.watches = ev.watches || [];
      P.skew = (ev.now || Date.now() / 1000) - Date.now() / 1000; renderNext(); return;
    case "vitals": return vitals(ev);
    case "you": closeLive(); addMsg("you", ev.text); setBusy(true); break;
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
      if (!replay && P.speak && said) speakOut(said);
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
function empty() { $("empty").hidden = $("chat").children.length > 0; }
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
  $("stateText").textContent = state === "offline" ? "Not connected" : AT_HOME[state] || AT_HOME.idle;
  const c = COLORS[state] || COLORS.idle;
  document.documentElement.style.setProperty("--glow", c.join(", "));
  $("dot").classList.toggle("busy", state === "thinking" || state === "speaking");
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
  $("stopBtn").onclick = () => send({ type: "cancel" });
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
