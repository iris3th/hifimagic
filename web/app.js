"use strict";

let state = {};
let view = "now";
let seeking = false;
let progress = { position: 0, duration: 0, at: Date.now(), playing: false };
let toastTimer = null;

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtTime(seconds) {
  const n = Math.max(0, Math.floor(Number(seconds) || 0));
  const m = Math.floor(n / 60);
  return `${m}:${String(n % 60).padStart(2, "0")}`;
}

function toast(message, error = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), 3500);
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    state = await response.json();
    render();
  } catch (error) {
    toast(`Cannot reach controller: ${error.message}`, true);
  }
}

async function act(name, params = {}, message = null) {
  try {
    const response = await fetch("/api/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: name, params }),
    });
    const body = await response.json();
    if (!response.ok || body.error) throw new Error(body.error || "Action failed");
    if (message) toast(message);
    await refresh();
    return true;
  } catch (error) {
    toast(error.message, true);
    return false;
  }
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.addEventListener("state", () => refresh());
  source.addEventListener("hello", () => refresh());
  source.onerror = () => {
    const pill = $("conn-pill");
    pill.className = "pill off";
    pill.innerHTML = "<i></i>Controller offline";
  };
}

/* --------------------------------------------------------------- rendering */

function render() {
  const device = state.device || {};
  $("device-name").textContent = device.name || "CXN100";
  const pill = $("conn-pill");
  if (state.connected) {
    pill.className = "pill on";
    pill.innerHTML = "<i></i>Connected to " + esc(device.model || "player");
  } else {
    pill.className = "pill off";
    pill.innerHTML = "<i></i>Player offline";
  }

  const powered = (state.zone || {}).power === true || (state.power || {}).power === "ON";
  $("power-btn").classList.toggle("active", powered);
  renderVolume();
  renderMini();

  const playback = state.playback || {};
  const md = playback.metadata || {};
  const display = (state.now_playing || {}).display || {};
  progress = {
    position: Number(playback.position ?? display.progress?.position ?? 0),
    duration: Number(md.duration ?? display.progress?.duration ?? 0),
    at: Date.now(),
    playing: playback.state === "play",
  };

  if (view === "now") renderNow();
  if (view === "sources") renderSources();
  if (view === "presets") renderPresets();
  if (view === "queue") renderQueue();
  if (view === "radio") renderRadio();
  if (view === "settings" && !$("view-settings").contains(document.activeElement)) renderSettings();
}

function renderMini() {
  const md = (state.playback || {}).metadata || {};
  $("mini-title").textContent = md.title || (state.now_playing || {}).display?.line1 || "Nothing playing";
  $("mini-artist").textContent = md.artist || (state.now_playing || {}).display?.line2 || "CXN100";
}

function renderVolume() {
  const zone = state.zone || {};
  const canVolume = zone.pre_amp_mode === true || (zone.cbus && zone.cbus !== "off");
  const widget = $("volume-widget");
  if (!canVolume) {
    widget.innerHTML = "<span>Volume: amplifier controlled</span>";
    return;
  }
  const level = zone.volume_percent ?? 0;
  const mute = zone.mute === true;
  widget.innerHTML = `
    <span>🔊</span>
    <input type="range" id="volume-range" min="0" max="100" value="${esc(level)}" aria-label="Volume">
    <span id="volume-label">${mute ? "Muted" : esc(level) + "%"}</span>
    <button class="text-btn" id="mute-btn" title="Mute">${mute ? "Unmute" : "Mute"}</button>`;
  $("volume-range").addEventListener("change", (event) => act("volume_set", { value: Number(event.target.value) }));
  $("mute-btn").addEventListener("click", () => act("mute", { value: !mute }));
}

function currentArt() {
  const presets = state.presets?.presets || [];
  const active = presets.find((p) => p.is_playing) || presets.find((p) => (state.playback || {}).metadata?.name === p.name);
  return active?.art_url || (state.now_playing || {}).display?.art_url || null;
}

function renderNow() {
  const playback = state.playback || {};
  const md = playback.metadata || {};
  const now = state.now_playing || {};
  const display = now.display || {};
  const controls = new Set(now.controls || []);
  const title = md.title || display.line1 || "Nothing playing";
  const artist = md.artist || display.line2 || "";
  const album = md.album || display.line3 || "";
  $("now-title").textContent = title;
  $("now-artist").textContent = artist;
  $("now-album").textContent = album;
  $("source-badge").textContent = (now.source || {}).name || md.source || "—";
  $("state-badge").textContent = (playback.state || "idle").replace("_", " ");
  $("listening-on").textContent = now.listening_on || "—";
  $("playback-source").textContent = display.playback_source || "";

  const artwork = $("artwork");
  const art = currentArt();
  artwork.innerHTML = art
    ? `<img src="${esc(art)}" alt="Cover art" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'art-placeholder',textContent:'♪'}))">`
    : `<div class="art-placeholder">♪</div>`;

  const enabled = (name) => controls.size === 0 || controls.has(name);
  $("play-btn").textContent = playback.state === "play" ? "❚❚" : "▶";
  $("play-btn").disabled = !enabled("play_pause") && !enabled("pause") && !enabled("play");
  $("prev-btn").disabled = !enabled("track_previous");
  $("next-btn").disabled = !enabled("track_next");
  $("shuffle-btn").disabled = !enabled("toggle_shuffle");
  $("repeat-btn").disabled = !enabled("toggle_repeat");
  $("stop-btn").disabled = !enabled("stop");
  $("shuffle-btn").classList.toggle("active", playback.mode_shuffle && playback.mode_shuffle !== "off");
  $("repeat-btn").classList.toggle("active", playback.mode_repeat && playback.mode_repeat !== "off");

  const queue = now.queue || {};
  $("queue-info").textContent = queue.length
    ? `Track ${Number(queue.position ?? -1) + 1} of ${queue.length}`
    : "";

  const signal = [
    ["Codec", md.codec],
    ["Format", md.encoding || md.sample_format],
    ["Sample rate", md.sample_rate ? `${(md.sample_rate / 1000).toFixed(1)} kHz` : null],
    ["Bit depth", md.bit_depth ? `${md.bit_depth} bit` : null],
    ["Bitrate", md.bitrate ? `${(md.bitrate / 1000).toFixed(0)} kbps` : null],
    ["Lossless", md.lossless === true ? "Yes" : md.lossless === false ? "No" : null],
    ["MQA", md.mqa && md.mqa !== "none" ? md.mqa : null],
    ["Track", md.track_number ? `#${md.track_number}` : null],
  ].filter(([, value]) => value);
  $("signal-list").innerHTML = signal.map(([key, value]) =>
    `<div class="dl-row"><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("") ||
    `<div class="dl-row"><dt>Signal</dt><dd>No data</dd></div>`;
  updateProgress();
}

function updateProgress() {
  const seek = $("seek");
  if (!seek) return;
  const elapsed = progress.playing
    ? progress.position + (Date.now() - progress.at) / 1000
    : progress.position;
  const duration = progress.duration || 0;
  if (!seeking) {
    seek.max = String(Math.max(1, duration));
    seek.value = String(Math.min(duration || 1, Math.max(0, elapsed)));
    $("pos-text").textContent = fmtTime(elapsed);
  }
  $("dur-text").textContent = fmtTime(duration);
}

const SOURCE_ICONS = {
  IR: "◉", USB_AUDIO: "⇄", SPDIF_COAX: "◎", SPDIF_TOSLINK: "◇",
  MEDIA_PLAYER: "♫", AIRPLAY: "⌁", SPOTIFY: "◍", BLUETOOTH: "⏣",
  CAST: "▣", ROON: "◈", TIDAL: "▲", QPLAY: "◐", QOBUZ: "◆",
};

function renderSources() {
  const sources = state.sources?.sources || [];
  const current = (state.zone || {}).source;
  $("source-grid").innerHTML = sources.filter((s) => s.ui_selectable !== false).map((s) => `
    <button class="source-card ${s.id === current ? "current" : ""}" data-id="${esc(s.id)}">
      <span class="source-icon">${SOURCE_ICONS[s.id] || "♪"}</span>
      <span><strong>${esc(s.name || s.default_name)}</strong><small>${esc(s.description || s.id)}</small></span>
    </button>`).join("");
  document.querySelectorAll(".source-card").forEach((button) => {
    button.addEventListener("click", () => act("source", { id: button.dataset.id }, `Switched to ${button.textContent.trim()}`));
  });
}

function presetCard(preset) {
  const art = preset.art_url
    ? `<img class="preset-art" src="${esc(preset.art_url)}" alt="">`
    : `<span class="preset-art">♪</span>`;
  return `<div class="preset-card ${preset.is_playing ? "playing" : ""}">
      ${art}
      <div class="preset-body">
        <strong>${esc(preset.name)}</strong>
        <small>${esc(preset.type || "Stream")} · preset ${esc(preset.id)}</small>
      </div>
      <div class="preset-actions">
        <button data-rename="${esc(preset.id)}" title="Rename">Rename</button>
        <button data-delete="${esc(preset.id)}" title="Delete">Delete</button>
      </div>
      <button class="play-preset" data-play="${esc(preset.id)}" title="Play">▶</button>
    </div>`;
}

function bindPresetCards(container) {
  container.querySelectorAll("[data-play]").forEach((button) => button.addEventListener("click", () =>
    act("preset", { preset: Number(button.dataset.play) })));
  container.querySelectorAll("[data-rename]").forEach((button) => button.addEventListener("click", async () => {
    const id = Number(button.dataset.rename);
    const preset = (state.presets?.presets || []).find((p) => p.id === id);
    const name = prompt("New preset name", preset?.name || "");
    if (name && await act("rename_preset", { preset: id, name }, "Preset renamed")) refresh();
  }));
  container.querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", async () => {
    const id = Number(button.dataset.delete);
    const preset = (state.presets?.presets || []).find((p) => p.id === id);
    if (confirm(`Delete preset ${id} (${preset?.name || ""})?`)) await act("delete_preset", { preset: id }, "Preset deleted");
  }));
}

function renderPresets() {
  const term = ($("preset-search").value || "").toLowerCase();
  const presets = (state.presets?.presets || []).filter((p) => (p.name || "").toLowerCase().includes(term));
  $("preset-grid").innerHTML = presets.length
    ? presets.map(presetCard).join("")
    : `<p class="muted">No presets found.</p>`;
  bindPresetCards($("preset-grid"));
}

function renderRadio() {
  const stations = (state.presets?.presets || []).filter((p) =>
    (p.type === "Radio" || p.class === "stream.radio") && p.airable_radio_id);
  $("radio-grid").innerHTML = stations.length
    ? stations.map(presetCard).join("")
    : `<p class="muted">No saved radio presets.</p>`;
  bindPresetCards($("radio-grid"));
}

function renderQueue() {
  const queue = state.queue || {};
  const items = queue.items || [];
  const current = queue.play_postition ?? (state.playback || {}).queue_index ?? -1;
  $("queue-summary").textContent = `${queue.total ?? items.length} item${items.length === 1 ? "" : "s"}`;
  $("queue-list").innerHTML = items.length ? items.map((item) => {
    const md = item.metadata || {};
    const position = Number(item.position ?? 0);
    return `<div class="queue-row ${position === current ? "current" : ""}">
        <span class="qnum">${position === current ? "▶" : position + 1}</span>
        <span class="qtitle">${esc(md.title || md.name || "Unknown")}<small>${esc(md.artist || "")}</small></span>
        <span class="qalbum">${esc(md.album || "")}</span>
        <button data-index="${esc(position)}">Play</button>
      </div>`;
  }).join("") : `<p class="muted">The queue is empty.</p>`;
  $("queue-list").querySelectorAll("[data-index]").forEach((button) =>
    button.addEventListener("click", () => act("queue_index", { index: Number(button.dataset.index) })));
}

function toggleHtml(value, action, extra = {}) {
  return `<button class="toggle ${value ? "on" : ""}" data-toggle="${esc(action)}" data-extra='${esc(JSON.stringify(extra))}' aria-label="Toggle"></button>`;
}

function renderSettings() {
  const audio = state.audio || {};
  const zone = state.zone || {};
  const power = state.power || {};
  const display = state.display || {};
  const update = state.update || {};
  const device = state.device || {};
  const specs = state.specs || {};
  const preAmpWritable = specs.zone?.pre_amp_mode?.readonly !== true;
  const balanceWritable = specs.audio?.balance?.readonly === false;
  const outputs = state.outputs?.outputs || [];

  $("volume-settings").innerHTML = `
    <div class="setting-row"><label>Internal pre-amp</label>${toggleHtml(zone.pre_amp_mode === true, "pre_amp")}</div>
    ${preAmpWritable ? "" : `<p class="hint">Pre-amp mode is read-only on this configuration.</p>`}
    <div class="setting-row"><label>Control bus</label>
      <select data-setting="control_bus">
        ${["off", "amplifier", "receiver"].map((v) => `<option value="${v}" ${zone.cbus === v ? "selected" : ""}>${v}</option>`).join("")}
      </select></div>
    <div class="setting-row"><label>Volume limit</label>
      <input type="number" min="1" max="100" data-setting="volume_limit" value="${esc(audio.volume_limit_percent ?? 100)}"></div>
    ${outputs.length ? `<div class="setting-row"><label>Audio output</label>
      <select data-setting="audio_output">${outputs.map((o) => `<option value="${esc(o.id)}" ${o.id === zone.audio_output ? "selected" : ""}>${esc(o.name)}</option>`).join("")}</select></div>` : ""}
    ${balanceWritable ? `<div class="setting-row"><label>Balance</label><input type="number" min="-15" max="15" data-setting="balance" value="${esc(audio.balance ?? 0)}"></div>` : ""}
    ${!balanceWritable && audio.balance !== undefined ? `<p class="hint">Balance: ${esc(audio.balance)} (controlled by amplifier)</p>` : ""}`;

  const eq = audio.user_eq || {};
  const bands = eq.bands || [];
  const labels = ["80 Hz", "120 Hz", "315 Hz", "800 Hz", "2 kHz", "5 kHz", "8 kHz"];
  $("eq-settings").innerHTML = `
    <div class="setting-row"><label>User equaliser</label>${toggleHtml(eq.enabled === true, "eq_enable")}</div>
    <div class="eq-presets">${Object.keys(EQ_PRESETS).map((name) => `<button data-eq-preset="${name}">${name.replace("_", " ")}</button>`).join("")}</div>
    ${bands.map((band, index) => `
      <div class="eq-band">
        <span>${labels[index] || esc(band.freq)}</span>
        <input type="range" min="-6" max="3" step="0.1" value="${esc(band.gain)}" data-band="${esc(band.index)}">
        <span id="band-label-${esc(band.index)}">${Number(band.gain) > 0 ? "+" : ""}${esc(band.gain)} dB</span>
      </div>`).join("")}
    <div class="setting-row"><label>Room correction</label>${toggleHtml(audio.tilt_eq?.enabled === true, "tilt_enable")}</div>
    <div class="setting-row"><label>Correction intensity</label>
      <input type="range" min="-15" max="15" data-setting="tilt_intensity" value="${esc(audio.tilt_eq?.intensity ?? 0)}"></div>`;

  $("display-settings").innerHTML = `
    <div class="setting-row"><label>Display brightness</label>
      <select data-setting="display_brightness">
        ${["off", "dim", "bright"].map((v) => `<option value="${v}" ${display.brightness === v ? "selected" : ""}>${v}</option>`).join("")}
      </select></div>`;

  $("power-settings").innerHTML = `
    <div class="setting-row"><label>Standby mode</label>
      <select data-setting="standby_mode">
        ${["NETWORK", "ECO_MODE"].map((v) => `<option value="${v}" ${power.standby_mode === v ? "selected" : ""}>${v === "NETWORK" ? "Network standby" : "Eco (no network)"}</option>`).join("")}
      </select></div>
    <div class="setting-row"><label>Auto power down (seconds)</label>
      <input type="number" min="0" max="7200" step="60" data-setting="auto_power_down" value="${esc(power.auto_power_down ?? 1200)}"></div>
    <div class="setting-row"><label>Power</label>
      <span><button class="text-btn" data-power="power_on">On</button> · <button class="text-btn" data-power="standby">Standby</button></span></div>`;

  $("device-settings").innerHTML = `
    <div class="setting-row"><label>Device name</label>
      <input type="text" data-setting="set_name" value="${esc(device.name || "")}"></div>
    <div class="setting-row"><label>Firmware update</label><span>${update.update_available ? "Available" : "Up to date"}${update.updating ? " (installing)" : ""}</span></div>
    <div class="setting-row"><label>Early update channel</label>${toggleHtml(update.early_update === true, "early_update")}</div>
    <p class="hint">API ${esc(device.api || "?")} · model ${esc(device.model || "?")} · unit ${esc(device.unit_id || "?")}</p>
    <button class="text-btn" id="check-update">Check for updates</button>`;

  const net = state.network || {};
  const ipv4 = net.ip_info?.ipv4 || {};
  $("network-settings").innerHTML = `
    <div class="dl-row"><dt>Connection</dt><dd>${esc(net.connection || "unknown")}</dd></div>
    <div class="dl-row"><dt>IP address</dt><dd>${esc(ipv4.address || "—")}</dd></div>
    <div class="dl-row"><dt>Gateway</dt><dd>${esc(ipv4.gateway || "—")}</dd></div>
    <div class="dl-row"><dt>DHCP</dt><dd>${net.ip_info?.dhcp ? "Yes" : "No"}</dd></div>`;

  bindSettings();
}

const EQ_PRESETS = {
  flat: [0, 0, 0, 0, 0, 0, 0], bass_boost: [3, 3, 1, 0, -1, -0.5, -0.3],
  bass_reduction: [-4.6, -1.8, -0.6, 0, 0.6, 0.4, 0], voice_clarity: [-6, -3.4, 3, 3, 3, 2.2, -1.4],
  treble_boost: [0, 0, 0, 0, 0.6, 1.8, 3], treble_reduction: [0, 0, 0, 0, 0, -1.2, -4.2],
  tv: [-1.9, -0.8, 1, 1, 0.8, 0, -0.8], movie: [0, 1.4, -0.4, -2, -0.6, 0.6, 1.1],
  gaming: [3, 3, 1, -1, -1, 0.6, -0.2],
};

function bindSettings() {
  document.querySelectorAll("[data-toggle]").forEach((button) => button.addEventListener("click", () => {
    const extra = JSON.parse(button.dataset.extra || "{}");
    act(button.dataset.toggle, { ...extra, value: !button.classList.contains("on") });
  }));

  const numberSettings = { volume_limit: "volume_limit", auto_power_down: "auto_power_down", tilt_intensity: "tilt_intensity", balance: "balance" };
  const selectSettings = { control_bus: "control_bus", audio_output: "audio_output", display_brightness: "display_brightness", standby_mode: "standby_mode" };

  document.querySelectorAll("[data-setting]").forEach((element) => {
    const key = element.dataset.setting;
    if (key === "set_name") {
      element.addEventListener("change", () => act("set_name", { name: element.value }, "Device name saved"));
    } else if (key === "tilt_intensity") {
      element.addEventListener("input", () => {
        const label = element.parentElement.querySelector("span");
        if (label) label.textContent = element.value;
      });
      element.addEventListener("change", () => act("tilt_intensity", { value: Number(element.value) }));
    } else if (numberSettings[key]) {
      element.addEventListener("change", () => act(numberSettings[key], { value: Number(element.value) }));
    } else if (selectSettings[key]) {
      element.addEventListener("change", () => act(selectSettings[key], { value: element.value }));
    }
  });

  document.querySelectorAll("[data-band]").forEach((slider) => {
    slider.addEventListener("input", () => {
      const label = $("band-label-" + slider.dataset.band);
      if (label) label.textContent = (Number(slider.value) > 0 ? "+" : "") + Number(slider.value).toFixed(1) + " dB";
    });
    slider.addEventListener("change", () => {
      const bands = (state.audio?.user_eq?.bands || []).map((band) => ({ ...band }));
      const band = bands.find((b) => String(b.index) === slider.dataset.band);
      if (band) band.gain = Number(slider.value) * 10;
      act("eq_bands", { bands });
    });
  });

  document.querySelectorAll("[data-eq-preset]").forEach((button) =>
    button.addEventListener("click", () => act("eq_preset", { preset: button.dataset.eqPreset }, `EQ: ${button.textContent}`)));
  document.querySelectorAll("[data-power]").forEach((button) =>
    button.addEventListener("click", () => act(button.dataset.power)));
  if ($("check-update")) $("check-update").addEventListener("click", () => act("check_update", {}, "Update check requested"));
}

/* ------------------------------------------------------------------ startup */

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  view = button.dataset.view;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("active", section.id === "view-" + view));
  render();
}));

$("play-btn").addEventListener("click", () => act("toggle"));
$("prev-btn").addEventListener("click", () => act("previous"));
$("next-btn").addEventListener("click", () => act("next"));
$("stop-btn").addEventListener("click", () => act("stop"));
$("shuffle-btn").addEventListener("click", () => act("shuffle", { value: "toggle" }));
$("repeat-btn").addEventListener("click", () => act("repeat", { value: "toggle" }));
$("power-btn").addEventListener("click", () => {
  const powered = (state.zone || {}).power === true || (state.power || {}).power === "ON";
  act(powered ? "standby" : "power_on");
});
$("preset-search").addEventListener("input", renderPresets);
$("radio-play").addEventListener("click", () => {
  const name = $("radio-name").value.trim() || "Internet Radio";
  const url = $("radio-url").value.trim();
  if (!url) return toast("Enter a stream URL first", true);
  act("radio_url", { name, url }, "Starting stream");
});
$("seek").addEventListener("pointerdown", () => (seeking = true));
$("seek").addEventListener("input", () => {
  $("pos-text").textContent = fmtTime($("seek").value);
});
$("seek").addEventListener("change", (event) => {
  seeking = false;
  act("seek", { position: Number(event.target.value) });
});

setInterval(updateProgress, 1000);
connectEvents();
refresh();
