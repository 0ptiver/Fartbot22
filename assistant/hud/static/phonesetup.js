// The Phone page: set up phone access (password + authenticator), switch it on/off, and see
// or sign out the phones that are signed in. The secrets never come back to this page.
"use strict";

const NovaPhone = (() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };
  let send = () => {};
  let info = null;
  let inSetup = false;

  function ago(ts) {
    const s = Date.now() / 1000 - ts;
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }

  function render(ev) {
    info = ev;
    const st = $("phoneState");
    const ready = ev.configured && ev.enabled && ev.running;
    st.textContent = !ev.configured ? "not set up" : !ev.enabled ? "off" : ev.running ? "on" : "waiting";
    st.className = "lock-state" + (ready ? " on" : ev.configured && ev.enabled ? " warn" : "");
    const ts = $("phoneTs");
    ts.textContent = ev.tailscale_up ? "✓ Tailscale is connected on this PC." : ev.tailscale
      ? "Tailscale is installed but not connected: open it and sign in." : "Tailscale isn't on this PC yet.";
    ts.className = "ts-state " + (ev.tailscale_up ? "ok" : "bad");

    const showSetup = !ev.configured || inSetup;
    $("phoneSetup").hidden = !showSetup;
    $("phoneReady").hidden = showSetup;
    const addrs = $("phoneAddrs");
    addrs.replaceChildren(...(ev.addresses || []).map((a) => el("li", "", a)));
    const wait = $("phoneWait");
    wait.hidden = !(ev.enabled && !ev.running) && !ev.locked_for && !ev.error;
    wait.textContent = ev.error ? ev.error
      : ev.locked_for ? `Sign-in is locked for ${Math.ceil(ev.locked_for / 60)} more minutes after wrong tries.`
      : ev.enabled && !ev.running ? "Waiting for Tailscale on this PC: open Tailscale and make sure it says Connected." : "";
    const note = $("phoneVoiceNote");
    note.hidden = !(ev.running && ev.voice_note);
    note.textContent = ev.running ? ev.voice_note || "" : "";
    $("phoneToggle").textContent = ev.enabled ? "Turn off" : "Turn on";
    $("phoneToggle").className = "btn" + (ev.enabled ? "" : " primary");

    const list = $("phoneDevices");
    list.replaceChildren();
    for (const d of ev.devices || []) {
      const li = el("li");
      const who = el("div");
      who.append(el("b", "", d.name), el("span", "", `Signed in ${ago(d.created)} · last used ${ago(d.last_seen)}`));
      const out = el("button", "btn", "Sign out");
      out.type = "button";
      out.onclick = () => send({ type: "phone_revoke", id: d.id });
      li.append(who, out);
      list.append(li);
    }
    $("phoneDevEmpty").hidden = (ev.devices || []).length > 0;
    $("phoneRevokeAll").hidden = (ev.devices || []).length < 2;
  }

  function setupShown(ev) {
    $("phonePwStep").hidden = true;
    $("phoneQrStep").hidden = false;
    $("phoneQr").src = ev.qr;                 // a data: URI made on the PC; never loaded from the web
    $("phoneSecret").textContent = ev.secret;
    $("phoneCode").value = "";
    $("phoneCode").focus();
  }

  function resetSetup() {
    $("phonePw").value = ""; $("phonePw2").value = "";
    $("phonePwStep").hidden = false;
    $("phoneQrStep").hidden = true;
    $("phoneQr").removeAttribute("src");
    $("phoneSecret").textContent = "";
  }

  const note = (text) => toast(text);        // hud.js's toast

  function init(sendFn) {
    send = sendFn;
    $("phoneNext").onclick = () => {
      const a = $("phonePw").value, b = $("phonePw2").value;
      if (a.length < 10) return note("Use at least 10 characters.");
      if (a !== b) return note("The two passwords don't match.");
      send({ type: "phone_setup", password: a });
      $("phonePw").value = ""; $("phonePw2").value = "";
    };
    $("phoneFinish").onclick = () => send({ type: "phone_confirm", code: $("phoneCode").value });
    $("phoneCode").addEventListener("keydown", (e) => { if (e.key === "Enter") $("phoneFinish").click(); });
    $("phoneCancel").onclick = () => { send({ type: "phone_cancel" }); inSetup = false; resetSetup(); if (info) render(info); };
    $("phoneToggle").onclick = () => send({ type: "phone_enable", on: !(info && info.enabled) });
    $("phoneRedo").onclick = () => { inSetup = true; resetSetup(); if (info) render(info); };
    $("phoneReset").onclick = () => {
      if (confirm("Turn phone access off, forget the password and authenticator, and sign out every phone?")) {
        send({ type: "phone_reset" });
      }
    };
    $("phoneRevokeAll").onclick = () => send({ type: "phone_revoke", id: null });
  }

  function handle(ev) {
    if (ev.type === "phone") render(ev);
    else if (ev.type === "phone_setup") setupShown(ev);
    else if (ev.type === "phone_done") { inSetup = false; resetSetup(); }
  }

  return { init, handle };
})();

document.addEventListener("DOMContentLoaded", () => NovaPhone.init((m) => send(m)));
