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
  timers: [], clockSkew: 0, showIgnored: false, retry: 0, chips: new Map(),
};

const PAGES = {
  home: ["Nova", "Everything Nova is doing, live."],
  chat: ["Chat", "Talk or type. Everything stays on this PC."],
  activity: ["Activity", "Every tool Nova used, and how it went."],
  timers: ["Timers & watches", "Timers, reminders, alarms and things Nova is watching for."],
  routines: ["Routines", "One phrase, several actions. Click one to run it."],
  brain: ["Brain", "What Nova remembers, and what it has learned from your corrections."],
  voice: ["Voice", "How Nova sounds, and who it listens to."],
  phone: ["Phone", "Reach Nova from your phone, privately, over Tailscale."],
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
  $("heroName").textContent = S.name.toUpperCase();
  $("confirmWho").textContent = S.name;
  document.title = S.name;
  $("askInput").placeholder = `Type to ${S.name}…`;
  greet();
  $("chat").replaceChildren();
  S.chips.clear();
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
  NovaHome.handle(ev, replay);
  switch (ev.type) {
    case "status": return status(ev);
    case "timers": return timersIn(ev);
    case "memories": NovaHome.memories(ev.items); return NovaBrain.memories(ev.items);
    case "media": return NovaHome.media(ev.items);
    case "lessons": return NovaHome.lessons(ev.items, send);
    case "navigate": if (!replay) selectTab(ev.page); return;
    case "memory_used": if (!replay) NovaBrain.used(ev.ids); return;
    case "toast": return toast(ev.text);
    case "vitals": return vitals(ev);
    case "voice": return NovaVoice.info(ev);
    case "phone": case "phone_setup": case "phone_done": return NovaPhone.handle(ev);
    case "remote":
      activity("ok", "Phone", ev.text, ev.ts);
      break;
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
      toolChip(ev.name);
      activity("run", pretty(ev.name), "", ev.ts);
      break;
    case "tool_done": {
      const item = lastRunning(ev.name);
      const detail = (ev.summary || "") + (ev.ms != null ? `  ·  ${(ev.ms / 1000).toFixed(1)} s` : "");
      if (item) finishActivity(item, ev.is_error ? "bad" : "ok", detail);
      else activity(ev.is_error ? "bad" : "ok", pretty(ev.name), detail, ev.ts);
      toolDone(ev.name, !!ev.is_error, ev.ms);
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
    case "voice_enrol":
      if (ev.done) addSys("sys", "🔒 Voice learned: voice lock is on");
      break;
    case "ignored":
      if (ev.voice && ev.near) activity("bad", "Didn't recognise your voice", `“${ev.text}”: ${ev.reason}`, ev.ts);
      else activity("ignored", `Heard “${ev.text}”`, ev.reason || "", ev.ts);
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

// Tools Nova uses show as small chips under the conversation, ticked off as they finish.
function toolChip(name) {
  let row = $("chat").lastElementChild;
  if (!row || !row.classList.contains("chips")) { row = el("li", "chips"); $("chat").append(row); }
  const chip = el("span", "chip");
  chip.append(el("i"), document.createTextNode(pretty(name)));
  row.append(chip);
  S.chips.set(name, chip);
  scrollChat();
}
function toolDone(name, bad, ms) {
  let chip = S.chips.get(name);
  if (!chip) { toolChip(name); chip = S.chips.get(name); }
  S.chips.delete(name);
  chip.classList.add(bad ? "bad" : "ok");
  if (bad) chip.lastChild.textContent = pretty(name) + " didn't work";
  if (ms != null) chip.append(el("span", "ms", `${(ms / 1000).toFixed(1)} s`));
}
function closeLive() {
  if (!S.live) return;
  S.live.classList.remove("live");
  if (!S.live.textContent.trim() && !S.live.classList.contains("cut")) S.live.remove();
  S.live = null;
}
function scrollChat() {
  const p = $("chatScroll");
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
  recording: () => `Watching what you do. Say “${S.name}, done” when you've finished`,
  enrolling: () => "Learning your voice: read the line Nova says",
  standby: () => `Standing down. Say “${S.name}, wake up” or press Wake up`,
  offline: () => "Not connected",
};
function status(ev) {
  S.state = ev.state; S.level = ev.level || 0; S.mode = ev.mode;
  S.standby = ev.standby; S.muted = ev.muted; S.subs = !!ev.subtitles;
  NovaVoice.lock(ev.lock);
  if (ev.confirm && !S.confirmShown) showConfirm(ev.confirm, false);
  if (!ev.confirm && S.confirmShown) hideConfirm();
  renderState();
}
function renderState() {
  $("stateText").textContent = (LABEL[S.state] || LABEL.idle)();
  $("muteBtn").setAttribute("aria-pressed", String(!!S.muted));
  $("muteBtn").title = S.muted ? "Microphone is off: click to turn it on" : "Turn the microphone off";
  $("muteLabel").textContent = S.muted ? "Mic off" : "Mic on";
  $("muteIcon").setAttribute("href", S.muted ? "#i-micoff" : "#i-mic");
  const stand = $("standBtn");
  $("standLabel").textContent = S.standby ? "Wake up" : "Stand down";
  stand.classList.toggle("wake", !!S.standby);
  $("ccBtn").setAttribute("aria-pressed", String(!!S.subs));
  const open = S.mode === "open_mic";
  $("modeLabel").textContent = open ? "Open mic" : "Wake word";
  $("modeBtn").classList.toggle("open", open);
  const c = COLORS[S.state] || COLORS.idle;
  document.documentElement.style.setProperty("--glow", c.join(", "));
  NovaHome.state((LABEL[S.state] || LABEL.idle)(), S);
}

const COLORS = {
  idle: [70, 180, 230], listening: [74, 222, 128], thinking: [167, 139, 250], speaking: [90, 216, 255],
  confirm: [255, 181, 71], standby: [100, 116, 139], muted: [248, 113, 113], offline: [70, 80, 100],
  dictation: [236, 240, 245], recording: [255, 77, 109], enrolling: [52, 211, 153],
};
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

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
  if (ev.routines && JSON.stringify(ev.routines) !== JSON.stringify(S.routines)) {
    S.routines = ev.routines;
    renderRoutines();
  }
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
  const card = (id, cls, title, sub, onCancel, cancelLabel) => {
    const li = el("li", cls);
    li.dataset.id = id;
    li.append(el("div", "label", title), el("div", "kind", sub), el("span", "left"));
    const x = el("button", "x", "✕");
    x.type = "button";
    x.title = cancelLabel;
    x.setAttribute("aria-label", `${cancelLabel}: ${title}`);
    x.onclick = onCancel;
    li.append(x);
    list.append(li);
    return li;
  };
  for (const t of S.timers) {
    keep.add(t.id);
    let li = existing.get(t.id);
    if (!li) {
      const at = new Date(t.due * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      li = card(t.id, "timer", t.text || (t.kind === "alarm" ? "Alarm" : t.kind === "timer" ? "Timer" : "Reminder"),
        `${t.kind} · ${at}`, () => send({ type: "cancel_timer", id: t.id }), "Cancel");
      const prog = el("div", "prog");
      prog.append(el("em"));
      li.append(prog);
    }
    const left = t.due - now;
    li.querySelector(".left").textContent = fmtLeft(left);
    li.classList.toggle("soon", left < 60);
    const total = t.created ? t.due - t.created : 0;
    const frac = total > 0 ? Math.min(1, Math.max(0, left / total)) : 1;
    li.querySelector(".prog em").style.transform = `scaleX(${frac})`;
  }
  for (const w of S.watches || []) {
    const id = "w:" + w.id;
    keep.add(id);
    if (!existing.get(id)) {
      const li = card(id, "timer watching", w.text.charAt(0).toUpperCase() + w.text.slice(1), "watch",
        () => send({ type: "cancel_watch", id: w.id }), "Stop watching");
      li.querySelector(".left").textContent = "Watching for it";
    }
  }
  for (const [id, li] of existing) if (!keep.has(id)) li.remove();
  const count = S.timers.length + (S.watches || []).length;
  $("timerBadge").hidden = count === 0;
  $("timerBadge").textContent = String(count);
  renderUpNext(now);
  empties();
}

// The next few timers and watches, always visible under the orb.
function renderUpNext(now) {
  NovaHome.next(S.timers, S.watches || [], now, fmtLeft);
  const items = [...S.timers].sort((a, b) => a.due - b.due).slice(0, 3);
  const watches = (S.watches || []).slice(0, Math.max(0, 4 - items.length));
  $("upNext").hidden = items.length + watches.length === 0;
  const list = $("nextList");
  list.replaceChildren();
  for (const t of items) {
    const li = el("li");
    li.append(el("span", "", t.text || t.kind.charAt(0).toUpperCase() + t.kind.slice(1)), el("b", "", fmtLeft(t.due - now)));
    list.append(li);
  }
  for (const w of watches) {
    const li = el("li", "w");
    li.append(el("span", "", w.text.charAt(0).toUpperCase() + w.text.slice(1)), el("b", "", "watching"));
    list.append(li);
  }
}

// --- this PC ---------------------------------------------------------------------------------
function vitals(ev) {
  NovaHome.vitals(ev);
  const show = (k, text, pct, warn, hot) => {
    const v = document.querySelector(`.vital[data-k="${k}"]`);
    if (!v) return;
    v.querySelector("b").textContent = text;
    v.querySelector("em").style.width = `${Math.max(0, Math.min(100, pct))}%`;
    v.classList.toggle("warn", pct >= warn && pct < hot);
    v.classList.toggle("hot", pct >= hot);
  };
  const pct = (x) => (x == null ? ["–", 0] : [`${x}%`, x]);
  show("cpu", ...pct(ev.cpu), 75, 92);
  show("ram", ...pct(ev.ram), 80, 92);
  show("vram", ...pct(ev.vram), 85, 95);
  if (ev.gpu_temp == null) show("gpu_temp", "–", 0, 101, 101);
  else show("gpu_temp", `${ev.gpu_temp}°C`, ev.gpu_temp, 80, 88);
}

// --- routines --------------------------------------------------------------------------------
function renderRoutines() {
  const box = $("routines");
  box.replaceChildren();
  for (const r of S.routines) {
    const card = el("div", "routine-card");
    const b = el("button", "routine" + (r.learned ? " learned" : ""));
    b.type = "button";
    const ic = el("span", "r-ic");
    ic.append(icon(r.learned ? "i-brain" : "i-bolt"));
    b.append(ic, el("b", "", r.label), el("span", "", (r.learned ? "taught · " : "") + `“${r.phrase}”`));
    b.onclick = () => send({ type: "routine", name: r.name });
    card.append(b);
    if (r.learned) {
      const x = el("button", "x", "✕");
      x.type = "button";
      x.title = "Forget this lesson";
      x.setAttribute("aria-label", `Forget ${r.label}`);
      x.onclick = () => { if (confirm(`Forget "${r.label}"?`)) send({ type: "routine_delete", name: r.name }); };
      card.append(x);
    }
    box.append(card);
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

function icon(id) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#" + id);
  svg.append(use);
  return svg;
}

function greet() {
  const h = new Date().getHours();
  const part = h < 5 ? "evening" : h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
  $("greeting").textContent = `Good ${part}${S.user ? ", " + S.user : ""}.`;
  for (const b of document.querySelectorAll("#suggest button")) b.title = `Ask ${S.name}`;
}

// --- UI wiring -------------------------------------------------------------------------------
function selectTab(name) {
  if (!PAGES[name]) name = "home";
  S.tab = name;
  document.body.dataset.page = name;
  for (const b of document.querySelectorAll(".rail button[data-tab]")) {
    if (b.dataset.tab === name) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  for (const p of document.querySelectorAll(".pages > .page")) p.hidden = p.id !== "tab-" + name;
  $("pageTitle").textContent = PAGES[name][0];
  $("pageSub").textContent = PAGES[name][1];
  NovaBrain.show(name === "brain");
  if (name === "activity") { S.unseenAct = 0; badge(); }
  if (name === "voice") send({ type: "voice_get" });
  if (name === "phone") send({ type: "phone_get" });
  if (name === "chat") { scrollChat(); if (!S.confirmShown) $("askInput").focus({ preventScroll: true }); }
  try { sessionStorage.setItem("novaTab", name); } catch (e) { /* ignore */ }
}

function init() {
  for (const b of document.querySelectorAll(".rail button[data-tab]")) b.onclick = () => selectTab(b.dataset.tab);
  for (const b of document.querySelectorAll("#suggest button")) {
    b.onclick = () => { send({ type: "text", text: b.dataset.say }); $("askInput").focus(); };
  }
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
  $("ccBtn").onclick = () => send({ type: "subtitles", on: !S.subs });
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
  let tab = "home";
  try { tab = sessionStorage.getItem("novaTab") || "home"; } catch (e) { /* ignore */ }
  setTimeout(() => selectTab(tab), 0);
  greet();
  renderState();
  NovaHome.init(send);
  NovaOrb.add($("orb"));
  NovaOrb.add($("heroOrb"));
  NovaOrb.start(() => ({ state: S.state, level: S.level, color: COLORS[S.state] || COLORS.idle }));
  if (!key) {
    showOffline("This window has no key.", "Open it with .venv\\Scripts\\python -m assistant hud");
    return;
  }
  connect();
}

document.addEventListener("DOMContentLoaded", init);
