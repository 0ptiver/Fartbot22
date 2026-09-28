// The dashboard (Home): Nova's bubble with live captions, what Nova is doing right now (a card
// per action, ticked off when done), and cards that light up when Nova touches them:
// Now playing, Up next, This PC, Remembers. Text only ever via textContent.
"use strict";

const NovaHome = (() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const svgIcon = (id) => {
    const ns = "http://www.w3.org/2000/svg";
    const s = document.createElementNS(ns, "svg");
    const u = document.createElementNS(ns, "use");
    u.setAttribute("href", "#" + id);
    s.append(u);
    return s;
  };
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  const q = (s) => (s ? `“${s}”` : "");

  // --- what each tool looks like in the Live feed -----------------------------------------
  // [icon, colour group, title from the arguments, the card it lights up]
  const TOOLS = {
    open_app: ["i-app", "blue", (a) => `Opening ${cap(a.name || "an app")}`],
    window: ["i-window", "blue", (a) => ({ focus: `Switching to ${cap(a.app || "it")}`, close: `Closing ${a.app === "this" ? "the window" : cap(a.app || "it")}`, quit: `Quitting ${cap(a.app || "it")}`,
      minimize: "Minimising the window", maximize: "Maximising the window", restore: "Restoring the window",
      show_desktop: "Showing the desktop", list: "Looking at open windows" })[a.action] || "Managing windows"],
    open_website: ["i-globe", "blue", (a) => a.search ? `Searching ${cap(a.site || "the web")} for ${q(a.search)}` : `Opening ${cap(a.site || "the browser")}`],
    play_music: ["i-music", "green", (a) => a.query ? `Playing ${q(a.query)} on Spotify` : a.kind === "liked_songs" ? "Playing your liked songs" : "Playing music", "wMedia"],
    music_control: ["i-music", "green", (a) => cap(String(a.action || "music").replace(/_/g, " ")), "wMedia"],
    media: ["i-music", "green", (a) => ({ play: "Resuming", pause: "Pausing", next: "Skipping ahead", previous: "Going back" })[a.action] || "Media", "wMedia"],
    now_playing: ["i-music", "green", () => "Checking what's playing", "wMedia"],
    queue_song: ["i-music", "green", (a) => `Queueing ${q(a.query)}`, "wMedia"],
    video: ["i-play", "green", (a) => "Video: " + (a.actions || []).map((x) => x.replace(/_/g, " ")).join(", "), "wMedia"],
    volume: ["i-volume", "green", (a) => a.action === "set" ? `Volume to ${a.level}%` : `Volume ${a.action}`],
    set_timer: ["i-clock", "amber", (a) => `Setting a ${a.hours ? a.hours + " h " : ""}${a.minutes ? a.minutes + " min " : ""}${a.seconds ? a.seconds + " s " : ""}timer`.replace(/\.0 /g, " "), "wNext"],
    set_reminder: ["i-clock", "amber", (a) => `Reminder: ${a.text || ""}`, "wNext"],
    set_alarm: ["i-clock", "amber", () => "Setting an alarm", "wNext"],
    cancel_timer: ["i-clock", "amber", () => "Cancelling a timer", "wNext"],
    watch: ["i-eye", "violet", (a) => `Watching: ${a.what || a.kind || "for it"}`, "wNext"],
    cancel_watch: ["i-eye", "violet", () => "Stopping a watch", "wNext"],
    web_search: ["i-search", "violet", (a) => `Searching the web for ${q(a.query)}`],
    escalate: ["i-spark", "violet", () => "Asking Claude"],
    work_it_out: ["i-spark", "violet", (a) => `Claude is working it out: ${q(a.task)}`],
    screenshot: ["i-eye", "violet", () => "Claude is looking at the screen"],
    screen_click: ["i-cursor", "violet", (a) => `Claude clicked at ${a.x}, ${a.y}`],
    summarize_page: ["i-spark", "violet", (a) => a.question ? "Asking Claude about this page" : "Having Claude read this page"],
    look_at_screen: ["i-eye", "violet", () => "Looking at your screen"],
    remember: ["i-brain", "pink", (a) => `Remembering ${q(a.text)}`, "wMem"],
    forget: ["i-brain", "pink", () => "Forgetting something", "wMem"],
    recall: ["i-brain", "pink", () => "Checking my memory", "wMem"],
    system_status: ["i-pulse", "blue", () => "Checking the PC", "wPc"],
    get_time: ["i-clock", "blue", (a) => a.place ? `Checking the time in ${cap(a.place)}` : "Checking the time"],
    brightness: ["i-spark", "amber", (a) => a.action === "get" ? "Checking the brightness" : a.action === "set" ? `Brightness to ${a.level}%` : `Brightness ${a.action}`],
    days_until: ["i-clock", "blue", (a) => `Counting the days to ${cap(a.what || "it")}`],
    empty_recycle_bin: ["i-power", "red", () => "Emptying the Recycle Bin"],
    lock_pc: ["i-lock", "red", () => "Locking the PC"],
    power: ["i-power", "red", (a) => cap(a.action || "Power")],
    type_text: ["i-keys", "blue", () => "Typing"],
    press_keys: ["i-keys", "blue", (a) => `Pressing ${a.keys || "keys"}`],
    click_element: ["i-cursor", "blue", (a) => `Clicking ${q(a.name || a.target)}`],
    mouse: ["i-cursor", "blue", () => "Using the mouse"],
    mouse_grid: ["i-cursor", "blue", (a) => a.action === "hide" ? "Hiding the grid" : "Showing the grid"],
    show_numbers: ["i-cursor", "blue", () => "Numbering what you can click"],
    dictation: ["i-keys", "blue", (a) => a.on ? "Dictation on" : "Dictation off"],
    run_routine: ["i-bolt", "amber", (a) => `Routine: ${a.name || ""}`],
    browser: ["i-globe", "blue", (a) => ({ open: `Opening ${a.site || "a page"}`, search: `Searching ${a.site ? a.site + " " : ""}for ${q(a.text)}`,
      click: `Clicking ${q(a.target)}`, type: `Typing ${q(a.text)}`, select: `Picking ${q(a.text)}`, press: `Pressing ${a.text || "a key"}`,
      scroll: `Scrolling ${a.text || "down"}`, back: "Going back a page", read: "Reading the page", look: "Looking at the page" })[a.action] || "Using the browser"],
    my_browser: ["i-globe", "blue", (a) => ({ new_tab: a.go ? `New tab: ${a.go}` : "Opening a new tab", go: `Going to ${a.go || "a page"}`,
      switch_tab: a.tab ? `Switching to the ${a.tab} tab` : `Switching to tab ${a.number}`, close_tab: a.tab ? `Closing the ${a.tab} tab` : "Closing the tab",
      next_tab: "Next tab", previous_tab: "Previous tab", reopen_tab: "Reopening the tab", back: "Going back", forward: "Going forward",
      reload: "Reloading the page", list_tabs: "Reading your tabs", find: `Finding ${q(a.text)}`, click: `Clicking ${q(a.text)}` })[a.action] || "Using your browser"],
    app: ["i-cursor", "blue", (a) => {
      const where = a.app ? ` in ${cap(a.app)}` : "";
      return ({ look: `Looking at ${a.app ? cap(a.app) : "the app"}`, click: `Clicking ${q(a.target)}${where}`, double_click: `Double-clicking ${q(a.target)}${where}`,
        right_click: `Right-clicking ${q(a.target)}${where}`, type: `Typing ${q(a.text)}${where}`, press: `Pressing ${a.keys || "keys"}${where}` })[a.action] || "Using an app";
    }],
    weather: ["i-globe", "blue", (a) => a.tomorrow ? "Checking tomorrow's weather" : `Checking the weather${a.place ? " in " + cap(a.place) : ""}`],
    calculate: ["i-spark", "violet", (a) => `Working out ${a.expression || "a sum"}`],
    set_location: ["i-globe", "pink", (a) => `Remembering you're in ${cap(a.city || "")}`],
    open_file: ["i-app", "blue", (a) => `Opening ${String(a.path || "a file").replace("~/", "")}`],
    show_page: ["i-home", "blue", (a) => `Showing ${a.page || "a page"}`],
    lessons: ["i-brain", "pink", (a) => a.action === "forget" ? "Forgetting a lesson" : "Listing what I've learned", "wMem"],
    set_voice: ["i-wave", "pink", () => "Changing my voice"],
    subtitles: ["i-cc", "violet", (a) => a.on ? "Subtitles on" : "Subtitles off"],
    create_note: ["i-keys", "blue", () => "Writing a note"],
    find_files: ["i-search", "blue", (a) => `Looking for ${q(a.query || a.name)}`],
  };
  const describe = (name, args) => {
    const t = TOOLS[name];
    let title;
    try { title = t ? t[2](args || {}) : null; } catch (e) { title = null; }
    return { icon: t ? t[0] : "i-bolt", tone: t ? t[1] : "blue", title: title || cap(String(name).replace(/_/g, " ")), card: t && t[3] };
  };

  // --- live feed -----------------------------------------------------------------------------
  const running = new Map();          // tool name -> [li, card]
  function toolStart(ev, replay) {
    const d = describe(ev.name, ev.input);
    const li = el("li", `live-item tone-${d.tone} run`);
    const ic = el("span", "live-ic");
    ic.append(svgIcon(d.icon));
    const mid = el("div", "live-mid");
    mid.append(el("b", "", d.title), el("span", "live-detail", "Working on it…"));
    const st = el("span", "live-st");
    li.append(ic, mid, st);
    const when = ev.ts ? new Date(ev.ts * 1000) : new Date();
    li.title = when.toLocaleTimeString();
    $("liveList").prepend(li);
    while ($("liveList").children.length > 7) $("liveList").lastChild.remove();
    running.set(ev.name, [li, d.card]);
    $("liveEmpty").hidden = true;
    if (!replay) pill(d.title);
  }
  function toolDone(ev, replay) {
    let [li, card] = running.get(ev.name) || [];
    if (!li) { toolStart(ev, true); [li, card] = running.get(ev.name); }
    running.delete(ev.name);
    li.classList.remove("run");
    li.classList.add(ev.is_error ? "bad" : "ok");
    const detail = (ev.summary || (ev.is_error ? "Didn't work" : "Done")).split("\n")[0].slice(0, 140);
    li.querySelector(".live-detail").textContent = detail + (ev.ms != null ? `  ·  ${(ev.ms / 1000).toFixed(1)} s` : "");
    li.querySelector(".live-st").replaceChildren(svgIcon(ev.is_error ? "i-x" : "i-check"));
    if (!replay && !ev.is_error && card) flash(card);
    if (!replay && !running.size) pill(null);
  }
  function flash(id) {
    const c = $(id);
    if (!c) return;
    c.classList.remove("pulse");
    void c.offsetWidth;                  // restart the animation
    c.classList.add("pulse");
  }
  let pillTimer = null;
  function pill(text) {                  // the top bar shows what Nova is doing on every page
    clearTimeout(pillTimer);
    if (text) { $("liveText").textContent = text; $("livePill").hidden = false; }
    else pillTimer = setTimeout(() => { $("livePill").hidden = true; }, 1500);
  }

  // --- captions ------------------------------------------------------------------------------
  let novaLive = false;
  function caption(ev, replay) {
    if (ev.type === "transcript") {
      $("capYou").textContent = ev.text;
      $("capNova").textContent = "";
      novaLive = false;
    } else if (ev.type === "text") {
      if (!novaLive) { $("capNova").textContent = ""; novaLive = true; }
      const t = $("capNova").textContent + ev.text;
      $("capNova").textContent = t.length > 260 ? "…" + t.slice(-258) : t;
      $("capNova").classList.add("speaking");
    } else if (ev.type === "speak" && ev.fixed && ev.show !== false) {
      $("capNova").textContent = ev.text;
    } else if (ev.type === "announcement") {
      $("capYou").textContent = "";
      $("capNova").textContent = "⏰ " + ev.text;
      if (!replay) flash("wNext");
    } else if (["turn_complete", "idle", "interrupted", "stopped", "error"].includes(ev.type)) {
      novaLive = false;
      $("capNova").classList.remove("speaking");
      if (ev.type === "error") $("capNova").textContent = ev.message || "Something went wrong.";
    }
  }

  // --- cards ---------------------------------------------------------------------------------
  function media(items) {
    const m = (items || [])[0];
    $("wMedia").classList.toggle("on", !!(m && m.status === "playing"));
    $("mediaTitle").textContent = m ? m.title : "Nothing playing";
    $("mediaArtist").textContent = m ? (m.artist || (m.status === "playing" ? "Playing" : "Paused")) : (items === null ? "Nova can see this on Windows" : "Say “Nova, play …”");
    $("mediaApp").textContent = m ? m.app : "";
    const playing = m && m.status === "playing";
    $("mediaToggle").dataset.media = playing ? "pause" : "play";
    $("mediaToggleIcon").setAttribute("href", playing ? "#i-pause" : "#i-play");
    const art = $("mediaArt");
    art.dataset.app = m ? m.app.toLowerCase() : "";
  }
  function next(timers, watches, now, fmt) {
    const list = $("homeNext");
    list.replaceChildren();
    const items = [...timers].sort((a, b) => a.due - b.due).slice(0, 4);
    for (const t of items) {
      const li = el("li");
      const left = t.due - now;
      if (left < 60) li.className = "soon";
      li.append(el("span", "", t.text || cap(t.kind)), el("b", "", fmt(left)));
      list.append(li);
    }
    for (const w of (watches || []).slice(0, Math.max(0, 5 - items.length))) {
      const li = el("li", "w");
      li.append(el("span", "", cap(w.text)), el("b", "", "watching"));
      list.append(li);
    }
    $("homeNextEmpty").hidden = list.children.length > 0;
  }
  function vitals(ev) {
    const C = 2 * Math.PI * 26;
    const show = (k, text, pct, warn, hot) => {
      const g = document.querySelector(`.gauge[data-k="${k}"]`);
      if (!g) return;
      g.querySelector("b").textContent = text;
      const p = Math.max(0, Math.min(100, pct || 0));
      g.querySelector(".fill").style.strokeDasharray = `${(p / 100) * C} ${C}`;
      g.classList.toggle("warn", pct >= warn && pct < hot);
      g.classList.toggle("hot", pct >= hot);
    };
    show("cpu", ev.cpu == null ? "–" : `${ev.cpu}%`, ev.cpu, 75, 92);
    show("ram", ev.ram == null ? "–" : `${ev.ram}%`, ev.ram, 80, 92);
    show("gpu_temp", ev.gpu_temp == null ? "–" : `${ev.gpu_temp}°`, ev.gpu_temp, 80, 88);
    show("vram", ev.vram == null ? "–" : `${ev.vram}%`, ev.vram, 85, 95);
  }
  let memSeen = null;
  function memories(items) {
    const list = $("memList");
    list.replaceChildren();
    const sorted = [...(items || [])].sort((a, b) => (b.created || 0) - (a.created || 0));
    for (const m of sorted.slice(0, 4)) list.append(el("li", "", m.text));
    nMem = (items || []).length;
    counts();
    $("memEmpty").hidden = sorted.length > 0;
    if (memSeen !== null && (items || []).length !== memSeen) flash("wMem");
    memSeen = (items || []).length;
  }
  let nLessons = null, nMem = 0;
  function counts() {
    const parts = [];
    if (nMem) parts.push(`${nMem} thing${nMem === 1 ? "" : "s"}`);
    if (nLessons) parts.push(`${nLessons} lesson${nLessons === 1 ? "" : "s"}`);
    $("memCount").textContent = parts.join(" · ");
  }
  function lessons(items, send) {
    const list = $("learnedList");
    list.replaceChildren();
    for (const x of items || []) {
      const li = el("li");
      const text = el("div");
      text.append(el("b", "", `“${x.said}”`), el("span", "", x.what.replace(/^“[^”]*”\s*(→|means)\s*/, (m, w) => w === "means" ? "means " : "→ ") +
        (x.uses ? `  ·  used ${x.uses}×` : "")));
      const del = el("button", "x", "✕");
      del.type = "button";
      del.title = "Forget this";
      del.setAttribute("aria-label", `Forget what you learned about ${x.said}`);
      del.onclick = () => send({ type: "lesson_delete", id: x.id });
      li.append(text, del);
      list.append(li);
    }
    const n = (items || []).length;
    $("learnedEmpty").hidden = n > 0;
    $("learnedCount").textContent = n ? `${n}` : "";
    if (nLessons !== null && n > nLessons) flash("wMem");
    nLessons = n;
    counts();
  }

  function state(label, s) {
    $("heroState").textContent = label;
    $("heroMute").setAttribute("aria-pressed", String(!!s.muted));
    $("heroMuteIcon").setAttribute("href", s.muted ? "#i-micoff" : "#i-mic");
    $("heroStand").classList.toggle("wake", !!s.standby);
    $("heroStand").title = s.standby ? "Wake up" : "Stand down";
  }

  // Voice lock refused a voice close to the owner's (a new headset?): say so, with a way to fix it.
  let sendFn = () => {};
  function voiceRefused(ev, replay) {
    if (!ev.voice || !ev.near || replay) return;
    const li = el("li", "live-item tone-red bad");
    const ic = el("span", "live-ic");
    ic.append(svgIcon("i-lock"));
    const mid = el("div", "live-mid");
    mid.append(el("b", "", "Didn't recognise your voice"),
      el("span", "live-detail", `“${ev.text}” · ${Math.round(ev.score * 100)}% match, needs ${Math.round(ev.threshold * 100)}%`));
    const btn = el("button", "btn primary me-btn", "That was me");
    btn.type = "button";
    btn.title = "Learn my voice on this microphone and do what I asked";
    btn.onclick = () => { btn.disabled = true; btn.textContent = "Learning…"; sendFn({ type: "voice_accept" }); };
    li.append(ic, mid, btn);
    $("liveList").prepend(li);
    while ($("liveList").children.length > 7) $("liveList").lastChild.remove();
    $("liveEmpty").hidden = true;
  }

  function handle(ev, replay) {
    if (ev.type === "ignored") voiceRefused(ev, replay);
    switch (ev.type) {
      case "tool": toolStart(ev, replay); break;
      case "tool_done": toolDone(ev, replay); break;
      case "interrupted": case "stopped": case "turn_complete":
        for (const [name] of running) toolDone({ name, is_error: false, summary: "Stopped" }, true);
        pill(null);
        break;
    }
    caption(ev, replay);
  }

  function init(send) {
    sendFn = send;
    $("heroStop").onclick = () => send({ type: "stop" });
    $("heroMute").onclick = () => $("muteBtn").click();
    $("heroStand").onclick = () => $("standBtn").click();
    for (const b of document.querySelectorAll("#wMedia [data-media]")) {
      b.onclick = () => send({ type: "media_cmd", action: b.dataset.media });
    }
    $("homeAsk").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = $("homeAskInput").value.trim();
      if (!v) return;
      send({ type: "text", text: v });
      $("homeAskInput").value = "";
    });
  }

  return { init, handle, media, next, vitals, memories, lessons, state, flash, describe };
})();
