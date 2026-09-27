// The Voice tab: mix Nova's voice from Kokoro's voices, adjust speed/pitch/accent, preview, save.
"use strict";

const NovaVoice = (() => {
  const V = { voices: [], design: null, presets: {} };

  function info(ev) {
    $("voiceUnavailable").hidden = !ev.unavailable;
    $("voiceDesigner").hidden = !!ev.unavailable;
    if (ev.unavailable) return;
    V.voices = ev.voices || [];
    V.presets = ev.presets || {};
    V.design = JSON.parse(JSON.stringify(ev.design));
    render();
  }

  function label(id) {
    const v = V.voices.find((x) => x.id === id);
    return v ? v.label : id;
  }

  function render() {
    const box = $("voicePresets");
    box.replaceChildren();
    for (const [name, p] of Object.entries(V.presets)) {
      if (!Object.keys(p.mix).every((id) => V.voices.some((v) => v.id === id))) continue;
      const b = el("button", "", name);
      b.type = "button";
      b.onclick = () => { V.design = JSON.parse(JSON.stringify(p)); render(); preview(); };
      box.append(b);
    }
    const mix = $("voiceMix");
    mix.replaceChildren();
    const entries = Object.entries(V.design.mix);
    const total = entries.reduce((a, [, w]) => a + w, 0) || 1;
    entries.forEach(([id, w]) => {
      const row = el("div", "mix-row");
      const sel = document.createElement("select");
      sel.setAttribute("aria-label", "Voice");
      for (const v of V.voices) {
        const o = el("option", "", v.label);
        o.value = v.id;
        o.selected = v.id === id;
        sel.append(o);
      }
      sel.onchange = () => {
        const next = {};
        for (const [k, val] of Object.entries(V.design.mix)) next[k === id ? sel.value : k] = val;
        V.design.mix = next;
        render();
      };
      const range = document.createElement("input");
      range.type = "range"; range.min = "5"; range.max = "100"; range.value = String(Math.round(w * 100));
      range.setAttribute("aria-label", `How much of ${label(id)}`);
      range.oninput = () => { V.design.mix[id] = Number(range.value) / 100; pct.textContent = pctOf(id); refreshPct(); };
      const pct = el("span", "pct", Math.round((w / total) * 100) + "%");
      pct.dataset.id = id;
      const x = el("button", "x", "✕");
      x.type = "button";
      x.disabled = entries.length === 1;
      x.title = "Remove this voice";
      x.onclick = () => { delete V.design.mix[id]; render(); };
      row.append(sel, range, pct, x);
      mix.append(row);
    });
    $("voiceAdd").hidden = entries.length >= 3;
    $("voiceSpeed").value = String(V.design.speed);
    $("voicePitch").value = String(V.design.pitch);
    $("voiceLang").value = V.design.lang;
    sliders();
  }

  function pctOf(id) {
    const total = Object.values(V.design.mix).reduce((a, b) => a + b, 0) || 1;
    return Math.round((V.design.mix[id] / total) * 100) + "%";
  }
  function refreshPct() {
    for (const s of $("voiceMix").querySelectorAll(".pct")) s.textContent = pctOf(s.dataset.id);
  }
  function sliders() {
    $("speedVal").textContent = Number($("voiceSpeed").value).toFixed(2) + "×";
    const p = Number($("voicePitch").value);
    $("pitchVal").textContent = (p > 0 ? "+" : "") + p + " semitones";
  }
  function current() {
    return { mix: V.design.mix, speed: Number($("voiceSpeed").value), pitch: Number($("voicePitch").value),
      lang: $("voiceLang").value };
  }
  function preview() { send({ type: "voice_preview", design: current(), text: $("voiceText").value }); }

  function init() {
    $("voiceSpeed").oninput = () => { V.design.speed = Number($("voiceSpeed").value); sliders(); };
    $("voicePitch").oninput = () => { V.design.pitch = Number($("voicePitch").value); sliders(); };
    $("voiceLang").onchange = () => { V.design.lang = $("voiceLang").value; };
    $("voiceAdd").onclick = () => {
      const unused = V.voices.find((v) => !(v.id in V.design.mix));
      if (unused) { V.design.mix[unused.id] = 0.3; render(); }
    };
    $("voicePreview").onclick = preview;
    $("voiceSave").onclick = () => send({ type: "voice_save", design: current() });
  }

  return { info, init };
})();

document.addEventListener("DOMContentLoaded", () => NovaVoice.init());
