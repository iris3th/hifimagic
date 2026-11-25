// SPDX-License-Identifier: AGPL-3.0-or-later
"use strict";

let state = {};
let view = "now";
let seeking = false;
let progress = { position: 0, duration: 0, at: Date.now(), playing: false };
let toastTimer = null;
let controllerConfig = { host: "", port: 80, connected: false, connecting: true, last_error: "" };
let splashManual = false;
let browseStack = [{ id: "0", title: "USB Drives" }];
let browseItems = [];
let browseMeta = { total: 0, next_start: 0, has_more: false };
let browseLoading = false;
let libraryLoaded = false;
let libraryView = "icons";
let advancedSettings = false;
try {
  libraryView = localStorage.getItem("hifimagic.library-view") === "list" ? "list" : "icons";
  advancedSettings = localStorage.getItem("hifimagic.advanced-settings") === "1";
} catch (_) { /* private mode */ }
let artistInfo = null;
let artistInfoKey = "";
let artistInfoArtist = "";
let artistInfoPending = "";
let artistInfoExpanded = false;
let artistInfoFetching = false;
let musicDb = { provider: "musicbrainz", custom_url: "", label: "MusicBrainz" };
let albumInfo = null;
let stationFilters = null;
let stationFavourites = [];
let favourites = [];
let favouriteKeys = new Set();
let stationResults = [];
let stationLoading = false;
let radioPageLoaded = false;
let aiConfigured = false;
let pendingNow = null;
let lastPowered = null;
const LIBRARY_NOTE = "Browse the USB drive through the player's UPnP media server on port 8050. Browsing is read-only. Use ⋮ on any album or folder to play it now, play it next, queue it, or add it to a saved playlist.";

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function playerPowerKnown() {
  return Object.keys(state.power || {}).length > 0 || Object.keys(state.zone || {}).length > 0;
}

function playerPowered() {
  const power = state.power || {};
  if ("power" in power) return power.power === "ON";
  return (state.zone || {}).power === true;
}

function deviceLabel() {
  const device = state.device || {};
  if (device.name) return String(device.name);
  if (controllerConfig.name) return String(controllerConfig.name);
  if (controllerConfig.host) return `Player at ${controllerConfig.host}`;
  return "Player";
}

function capability(name) {
  const caps = state.capabilities || {};
  if (!Object.prototype.hasOwnProperty.call(caps, name)) return true;
  return Boolean(caps[name]);
}

function starHtml(active, title) {
  return `<button class="row-btn star-btn ${active ? "saved" : ""}" ${title} title="${active ? "Remove favourite" : "Save favourite"}">${active ? "★" : "☆"}</button>`;
}

function favouriteKeyFor(item) {
  if (!item || typeof item !== "object") return "";
  if (item.key) return String(item.key);
  if (item.id) return `u:${item.id}`;
  const parts = [item.artist, item.album, item.title]
    .map((value) => String(value || "").trim()).filter(Boolean);
  return parts.length ? `n:${parts.join(" | ").toLocaleLowerCase()}` : "";
}

function isFavourite(item) {
  return favouriteKeys.has(favouriteKeyFor(item));
}

function libraryItemForMetadata(md) {
  const pools = [browseItems, discoverItems];
  for (const pool of pools) {
    const found = pool.find((item) => item.title === md.title &&
      (item.artist || "") === (md.artist || "") &&
      (item.album || "") === (md.album || ""));
    if (found) return found;
  }
  return null;
}

function favouritePayloadFor(item, fallbackKind = "track") {
  if (!item || typeof item !== "object") return null;
  const container = Boolean(item.is_container);
  return {
    key: favouriteKeyFor(item),
    kind: container ? "album" : fallbackKind,
    title: item.title || item.name || "",
    artist: item.artist || "",
    album: item.album || "",
    source: item.source || (container ? "USB album" : "USB track"),
    art_url: item.art_url || "",
    id: item.id || "",
    parent_id: item.parent_id || "",
    class: item.class || "",
    is_container: container,
    resource: item.resource || "",
  };
}

function favouriteToPlaylistItem(item) {
  return {
    key: item.key,
    kind: "media",
    id: item.id,
    parent_id: item.parent_id,
    title: item.title,
    artist: item.artist || "",
    album: item.album || "",
    source: item.source || (item.is_container ? "USB album" : "USB track"),
    class: item.class || "object.item.audioItem.musicTrack",
    is_container: Boolean(item.is_container),
    art_url: item.art_url || "",
  };
}

async function loadFavourites() {
  try {
    const body = await api("/api/favourites");
    favourites = body.items || [];
    favouriteKeys = new Set(favourites.map((item) => item.key));
  } catch (error) {
    toast(`Favourites unavailable: ${error.message}`, true);
  }
  if (view === "favourites") renderFavourites();
}

async function toggleFavourite(item, fallbackKind = "track") {
  const payload = favouritePayloadFor(item, fallbackKind);
  if (!payload || !payload.key) return toast("This item has no stable identity", true);
  const saving = !favouriteKeys.has(payload.key);
  try {
    const body = saving
      ? await postJSON("/api/favourites/add", { item: payload })
      : await postJSON("/api/favourites/remove", { key: payload.key });
    favourites = body.items || [];
    favouriteKeys = new Set(favourites.map((entry) => entry.key));
    render();
    toast(saving ? `Saved ${payload.title || "item"}` : `Removed ${payload.title || "item"}`);
  } catch (error) {
    toast(error.message, true);
  }
}

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

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok || body.error) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function postJSON(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function collectUiSettings() {
  let library_view = "icons";
  let working_playlist = null;
  try {
    library_view = localStorage.getItem("hifimagic.library-view") === "list" ? "list" : "icons";
    advancedSettings = localStorage.getItem("hifimagic.advanced-settings") === "1";
    const raw = localStorage.getItem(PLAYLIST_KEY);
    if (raw) working_playlist = JSON.parse(raw);
  } catch (_) { /* private mode or damaged local data */ }
  return { library_view, working_playlist, advanced_settings: advancedSettings };
}

function applyUiSettings(ui) {
  if (!ui || typeof ui !== "object") return;
  if (ui.library_view === "icons" || ui.library_view === "list") {
    libraryView = ui.library_view;
    try { localStorage.setItem("hifimagic.library-view", libraryView); } catch (_) { /* ignore */ }
  }
  if (typeof ui.advanced_settings === "boolean") {
    setAdvancedSettings(ui.advanced_settings);
  }
  if (ui.working_playlist && typeof ui.working_playlist === "object") {
    try { localStorage.setItem(PLAYLIST_KEY, JSON.stringify(ui.working_playlist)); } catch (_) { /* ignore */ }
  }
}

function setAdvancedSettings(value) {
  advancedSettings = Boolean(value);
  const view = $("view-settings");
  const button = $("advanced-toggle");
  if (view) view.classList.toggle("show-advanced", advancedSettings);
  if (button) {
    button.textContent = advancedSettings ? "Hide advanced settings" : "Show advanced settings";
    button.setAttribute("aria-expanded", String(advancedSettings));
  }
  try { localStorage.setItem("hifimagic.advanced-settings", advancedSettings ? "1" : "0"); } catch (_) { /* ignore */ }
}

async function exportBackup() {
  const status = $("backup-status");
  status.textContent = "Preparing backup…";
  try {
    const response = await fetch("/api/backup/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ui: collectUiSettings() }),
    });
    if (!response.ok) {
      let detail = {};
      try { detail = await response.json(); } catch (_) { /* ignore */ }
      throw new Error(detail.error || `Backup failed (${response.status})`);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "hifimagic-backup-" + new Date().toISOString().slice(0, 10) + ".zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = "Backup downloaded. Keep the zip somewhere safe.";
    toast("Backup downloaded");
  } catch (error) {
    status.textContent = "Backup failed: " + error.message;
    toast(error.message, true);
  }
}

async function importBackup(file) {
  if (!file) return;
  if (!confirm(`Restore HifiMagic data from ${file.name}? This replaces the matching local JSON files.`)) return;
  const status = $("backup-status");
  status.textContent = "Checking and restoring backup…";
  try {
    const response = await fetch("/api/backup/restore", {
      method: "POST",
      headers: { "Content-Type": "application/zip" },
      body: await file.arrayBuffer(),
    });
    const body = await response.json();
    if (!response.ok || body.error) throw new Error(body.error || `Restore failed (${response.status})`);
    applyUiSettings(body.ui);
    const count = (body.files || []).length;
    status.textContent = `Restored ${count} file${count === 1 ? "" : "s"}. Reloading…`;
    toast(`Restored ${count} file${count === 1 ? "" : "s"}`);
    setTimeout(() => location.reload(), 700);
  } catch (error) {
    status.textContent = "Restore failed: " + error.message;
    toast(error.message, true);
  }
}

function askChoice(title, message, choices) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "choice-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.innerHTML = `
      <div class="choice-card">
        <h3>${esc(title)}</h3>
        <p>${esc(message)}</p>
        <div class="choice-actions">
          ${choices.map((choice, index) => `
            <button class="${choice.primary ? "primary" : "row-btn"}"
              data-choice="${esc(choice.value)}"${index === 0 ? " autofocus" : ""}>
              ${esc(choice.label)}
            </button>`).join("")}
        </div>
      </div>`;
    document.body.appendChild(overlay);

    let finished = false;
    const finish = (value) => {
      if (finished) return;
      finished = true;
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(value);
    };
    const onKey = (event) => {
      if (event.key === "Escape") finish("cancel");
    };
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) return finish("cancel");
      const button = event.target.closest("[data-choice]");
      if (button) finish(button.dataset.choice);
    });
    document.addEventListener("keydown", onKey);
    const first = overlay.querySelector("button");
    if (first) first.focus();
  });
}

/* ------------------------------------------------------------ item menu */

let menuContext = null;

function closeItemMenu() {
  const menu = $("item-menu");
  if (menu) menu.hidden = true;
  menuContext = null;
}

function positionItemMenu(button) {
  const menu = $("item-menu");
  const rect = button.getBoundingClientRect();
  menu.hidden = false;
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  let left = rect.right - width;
  let top = rect.bottom + 6;
  if (top + height > window.innerHeight - 12) top = rect.top - height - 6;
  menu.style.left = `${Math.max(12, Math.min(left, window.innerWidth - width - 12))}px`;
  menu.style.top = `${Math.max(12, top)}px`;
}

function bindMenuAction(selector, handler) {
  const button = $("item-menu").querySelector(selector);
  if (button) button.addEventListener("click", handler);
}

function renderPlaylistSubmenu() {
  const menu = $("item-menu");
  const records = playlistRecords
    .filter((record) => record.kind !== "smart")
    .sort((a, b) => String(a.name).localeCompare(String(b.name)));
  menu.innerHTML = `
    <div class="menu-title">Add to playlist</div>
    <div class="menu-list">
      ${records.map((record) => `
        <button data-menu-playlist="${esc(record.id)}">♬ ${esc(record.name)}</button>`).join("") ||
        `<div class="menu-empty">No saved playlists yet.</div>`}
    </div>
    <div class="menu-sep"></div>
    <button class="menu-new" data-menu-new>＋ New playlist…</button>
    <button data-menu-back>← Back</button>`;
  menu.querySelectorAll("[data-menu-playlist]").forEach((button) =>
    button.addEventListener("click", () => addMenuItemToPlaylist({ id: button.dataset.menuPlaylist })));
  bindMenuAction("[data-menu-new]", async () => {
    const name = prompt("New playlist name:", "My playlist");
    if (name && name.trim()) await addMenuItemToPlaylist({ name: name.trim() });
  });
  bindMenuAction("[data-menu-back]", () => {
    menu.innerHTML = menuContext.html;
    bindItemMenuActions(menuContext.playable);
    positionItemMenu(menuContext.anchor);
  });
}

async function addMenuItemToPlaylist(target) {
  if (!menuContext) return;
  const payload = { item: menuContext.item, ...target };
  try {
    const body = await postJSON("/api/playlists/add", payload);
    await loadServerPlaylists();
    if (currentPlaylistId === body.playlist.id) await loadNamedPlaylist(body.playlist.id);
    closeItemMenu();
    toast(body.duplicate ? "Already in that playlist" : `Added to "${body.playlist.name}"`);
  } catch (error) {
    toast(error.message, true);
  }
}

function bindItemMenuActions(playable) {
  bindMenuAction("[data-menu-play]", () => {
    const item = menuContext?.item;
    closeItemMenu();
    if (item) mediaCommand(item, "media_play");
  });
  bindMenuAction("[data-menu-next]", () => {
    const item = menuContext?.item;
    closeItemMenu();
    if (item) mediaCommand(item, "media_play_next");
  });
  bindMenuAction("[data-menu-add]", () => {
    const item = menuContext?.item;
    closeItemMenu();
    if (item) mediaCommand(item, "media_add");
  });
  bindMenuAction("[data-menu-list]", () => {
    menuContext.html = $("item-menu").innerHTML;
    renderPlaylistSubmenu();
  });
}

function openItemMenu(button, item) {
  const menu = $("item-menu");
  const playable = isMediaPlayable(item);
  const clean = mediaPlaylistItem(item);
  menu.innerHTML = `
    <div class="menu-title">${esc(clean.title || "Item")}</div>
    ${playable ? `
      <button data-menu-play>▶ Play now</button>
      <button data-menu-next>⏭ Play next</button>
      <button data-menu-add>＋ Add to player queue</button>
      <div class="menu-sep"></div>` : ""}
    <button data-menu-list>♬ Add to playlist…</button>`;
  menuContext = { item: clean, playable, anchor: button, html: menu.innerHTML };
  bindItemMenuActions(playable);
  positionItemMenu(button);
}

document.addEventListener("click", (event) => {
  if ($("item-menu").hidden) return;
  const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
  const internal = path.some((node) =>
    node === $("item-menu") || (node instanceof Element && node.hasAttribute("data-item-menu")));
  if (internal) return;
  closeItemMenu();
});
document.addEventListener("scroll", closeItemMenu, true);

/* ------------------------------------------------------------ command palette */

let cmdResults = [];
let cmdTimer = null;
let cmdToken = 0;

const COMMANDS = [
  { label: "Play / pause", hint: "Transport", run: () => act("toggle") },
  { label: "Next track", hint: "Transport", run: () => act("next") },
  { label: "Previous track", hint: "Transport", run: () => act("previous") },
  { label: "Clear the player queue", hint: "Queue", run: () => act("queue_clear", {}, "Queue cleared") },
  { label: "Refresh USB library", hint: "Library", run: refreshUsb },
  { label: "Download local backup", hint: "Settings", run: exportBackup },
  { label: "Wake / standby", hint: "Power", run: () => act(playerPowered() ? "standby" : "power_on") },
  { label: "Save the queue as a playlist", hint: "Playlists", run: saveQueueAsPlaylist },
  { label: "Open Discover", hint: "Navigate", run: () => showView("discover") },
  { label: "Open Playlist", hint: "Navigate", run: () => showView("playlist") },
  { label: "Open Favourites", hint: "Navigate", run: () => showView("favourites") },
  { label: "Open Queue", hint: "Navigate", run: () => showView("queue") },
  { label: "Cancel sleep timer", hint: "Player", run: () => act("sleep_timer_cancel", {}, "Sleep timer cancelled") },
  { label: "Open mini player window", hint: "Window", run: () => openMiniWindow() },
  { label: "Re-scan USB library", hint: "Library", run: reindexLibrary },
];

function renderCommandPalette() {
  const list = $("cmd-results");
  if (!cmdResults.length) {
    list.innerHTML = `<p class="cmd-empty">No matches. Try a track, artist, album or command.</p>`;
    return;
  }
  list.innerHTML = cmdResults.map((result, index) => `
    <button class="cmd-item" data-cmdresult="${index}">
      <span class="cmd-kind">${esc(result.kind)}</span>
      <span class="cmd-label">${esc(result.label)}</span>
      <span class="cmd-sub">${esc(result.sub || "")}</span>
    </button>`).join("");
  list.querySelectorAll("[data-cmdresult]").forEach((button) =>
    button.addEventListener("click", () => runCommandResult(Number(button.dataset.cmdresult))));
}

async function refreshCommandPalette() {
  const query = $("cmd-input").value.trim();
  const token = ++cmdToken;
  const commands = COMMANDS
    .filter((command) => !query || command.label.toLowerCase().includes(query.toLowerCase()))
    .slice(0, 5)
    .map((command) => ({ kind: "command", label: command.label, sub: command.hint, run: command.run }));
  let tracks = [];
  if (query.length >= 2) {
    try {
      const body = await api(`/api/library/search?q=${encodeURIComponent(query)}&limit=6`);
      tracks = (body.items || []).map((item) => ({
        kind: "track", label: item.title, sub: item.artist || item.album || item.path, item,
      }));
    } catch (error) {
      // Keep command results usable if the index is unavailable.
    }
  }
  if (token !== cmdToken) return;
  cmdResults = [...commands, ...tracks];
  renderCommandPalette();
}

function openCommandPalette() {
  $("cmd-overlay").hidden = false;
  $("cmd-input").value = "";
  cmdResults = [];
  renderCommandPalette();
  $("cmd-input").focus();
}

function closeCommandPalette() {
  $("cmd-overlay").hidden = true;
  $("cmd-input").blur();
}

function runCommandResult(index) {
  const result = cmdResults[index];
  if (!result) return;
  closeCommandPalette();
  if (result.kind === "command") {
    result.run();
  } else if (result.kind === "track") {
    mediaCommand(result.item, "media_play");
  }
}

/* ------------------------------------------------------------ connection UI */

function setSplash(show, status = null, error = false) {
  $("splash").classList.toggle("show", show);
  if (status !== null) {
    const el = $("connect-status");
    el.textContent = status;
    el.className = "splash-status" + (error ? " error" : "");
  }
}

const SUPPORT_DEFAULT_CENTS = 1000;

function supportUrl() {
  const raw = String(controllerConfig.support_url || "").trim();
  if (!/^https?:\/\/\S+$/i.test(raw)) return "";
  try {
    const url = new URL(raw);
    url.searchParams.delete("prefilled_amount");
    url.searchParams.set("prefilled_amount", String(SUPPORT_DEFAULT_CENTS));
    return url.toString();
  } catch (_) {
    return raw;
  }
}

function applyVersion() {
  const tag = $("version-tag");
  const label = String(controllerConfig.version || "").trim();
  if (!tag || !label) return;
  tag.textContent = label.startsWith("v") ? label : `v${label}`;
  tag.title = `Running HifiMagic ${tag.textContent}`;
}

function applySupportLink() {
  const raw = String(controllerConfig.support_url || "").trim();
  const show = /^https?:\/\/\S+$/i.test(raw);
  const link = $("support-link");
  const sep = $("footer-sep");
  link.hidden = !show;
  sep.hidden = !show;
  if (show) {
    link.href = supportUrl();
    link.title = "Support HiFiMagic";
    link.setAttribute("aria-label", "Support HiFiMagic (opens in a new tab)");
  } else {
    link.removeAttribute("href");
    link.removeAttribute("title");
    link.removeAttribute("aria-label");
  }
}

async function loadConfig() {
  try {
    const response = await fetch("/api/config", { cache: "no-store" });
    controllerConfig = await response.json();
    applyVersion();
    applySupportLink();
    $("connect-host").value = controllerConfig.host || "";
    $("connect-port").value = controllerConfig.port || 80;
    if (controllerConfig.connected) {
      setSplash(false);
    } else {
      const message = controllerConfig.connecting
        ? "Looking for the player…"
        : (controllerConfig.last_error || "Enter the player address and click Connect.");
      setSplash(true, message, Boolean(controllerConfig.last_error));
    }
  } catch (error) {
    setSplash(true, `Cannot reach controller: ${error.message}`, true);
  }
}

async function connectFromForm(event) {
  event.preventDefault();
  const button = $("connect-button");
  button.disabled = true;
  setSplash(true, "Connecting…");
  try {
    const response = await fetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        host: $("connect-host").value.trim(),
        port: Number($("connect-port").value),
      }),
    });
    const body = await response.json();
    if (!response.ok || body.error) throw new Error(body.error || "Connection failed");
    controllerConfig = body.config;
    applySupportLink();
    splashManual = false;
    setSplash(false, `Connected to ${controllerConfig.host}:${controllerConfig.port}`);
    await refresh();
    toast(`Connected to ${controllerConfig.host}:${controllerConfig.port}`);
  } catch (error) {
    setSplash(true, error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function findPlayers() {
  const button = $("discover-button");
  button.disabled = true;
  setSplash(true, "Searching the local network for Cambridge Audio players…");
  try {
    const body = await api("/api/discover");
    const devices = body.devices || [];
    if (!devices.length) {
      setSplash(true, "No players found automatically. Enter the player IP and port below.", true);
      return;
    }
    const device = devices[0];
    $("connect-host").value = device.host;
    $("connect-port").value = device.port || 80;
    setSplash(true, `Found ${device.name} at ${device.host}:${device.port || 80}. Click Connect.`);
  } catch (error) {
    setSplash(true, `Discovery failed: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------ disk playlists */

const PLAYLIST_KEY = "hifimagic.playlist.v1";
const LEGACY_KEY = "hifimagic.playlists.v1";

function loadPlaylist() {
  try {
    const raw = localStorage.getItem(PLAYLIST_KEY);
    if (raw) {
      const data = JSON.parse(raw);
      if (data && Array.isArray(data.items)) {
        return { name: data.name || "My playlist", items: data.items };
      }
    }
  } catch (error) {
    console.warn("Could not load current playlist", error);
  }
  return { name: "My playlist", items: [] };
}

let playlist = loadPlaylist();
let playlistRecords = [];
let currentPlaylistId = "";

function savePlaylist() {
  try {
    localStorage.setItem(PLAYLIST_KEY, JSON.stringify(playlist));
  } catch (error) {
    console.warn("Could not cache current playlist", error);
  }
}

function recordById(id) {
  return playlistRecords.find((record) => record.id === id) || null;
}

async function migrateLegacyPlaylists() {
  let legacy = {};
  try {
    legacy = JSON.parse(localStorage.getItem(LEGACY_KEY) || "{}") || {};
  } catch (error) {
    return;
  }
  const names = Object.keys(legacy);
  for (const name of names) {
    try {
      await postJSON("/api/playlists", {
        playlist: { name, kind: "static", items: legacy[name] || [] },
      });
    } catch (error) {
      // Ignore invalid legacy entries.
    }
  }
  if (names.length) {
    const body = await api("/api/playlists");
    playlistRecords = body.playlists || [];
    toast(`Imported ${names.length} playlist(s) from this browser`);
  }
}

async function loadServerPlaylists() {
  try {
    const body = await api("/api/playlists");
    playlistRecords = body.playlists || [];
    const el = $("playlist-storage");
    if (el && body.path) el.textContent = `Playlists stored locally: ${body.path}`;
    if (!playlistRecords.length) await migrateLegacyPlaylists();
    renderPlaylistSelect();
  } catch (error) {
    console.warn("Could not load playlists", error);
  }
}

function renderPlaylistSelect() {
  const select = $("playlist-select");
  select.innerHTML = `<option value="">— New playlist —</option>` +
    playlistRecords.slice().sort((a, b) => a.name.localeCompare(b.name)).map((record) =>
      `<option value="${esc(record.id)}">${esc(record.name)} (${esc(record.count || 0)})</option>`
    ).join("");
  select.value = currentPlaylistId || "";
}

function newPlaylist() {
  currentPlaylistId = "";
  playlist = { name: "My playlist", items: [] };
  savePlaylist();
  renderPlaylistSelect();
  renderPlaylist();
}

async function saveNamedPlaylist() {
  const name = ($("playlist-name").value || "").trim() || "My playlist";
  const existing = recordById(currentPlaylistId);
  const payload = {
    name,
    kind: existing && existing.kind === "smart" ? "smart" : "static",
    items: playlist.items.map((item) => ({ ...item })),
    rules: existing ? existing.rules || {} : {},
    query: existing ? existing.query || "" : "",
  };
  if (existing && existing.kind === "smart") {
    payload.manual_items = playlist.items.map((item) => ({ ...item }));
  }
  if (existing) payload.id = existing.id;
  try {
    const body = await postJSON("/api/playlists", { playlist: payload });
    playlist.name = name;
    currentPlaylistId = body.playlist.id;
    savePlaylist();
    await loadServerPlaylists();
    renderPlaylist();
    toast(`Saved "${name}" (${playlist.items.length} items)`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadNamedPlaylist(id) {
  if (!id) return newPlaylist();
  try {
    const body = await api(`/api/playlists/${encodeURIComponent(id)}`);
    const record = body.playlist;
    playlist = { name: record.name, items: (record.items || []).map((item) => ({ ...item })) };
    currentPlaylistId = record.id;
    savePlaylist();
    renderPlaylist();
    renderPlaylistSelect();
    toast(`Loaded "${record.name}" (${playlist.items.length} items)`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function deleteNamedPlaylist() {
  const id = $("playlist-select").value;
  const record = recordById(id);
  if (!record) return toast("Choose a saved playlist first", true);
  if (!confirm(`Delete playlist "${record.name}"?`)) return;
  try {
    await postJSON("/api/playlists/delete", { id });
    if (currentPlaylistId === id) newPlaylist();
    await loadServerPlaylists();
    toast(`Deleted "${record.name}"`);
  } catch (error) {
    toast(error.message, true);
  }
}

/* ------------------------------------------------------------ discover / search */

let discoverItems = [];

async function loadIndexStatus() {
  try {
    const status = await api("/api/library/status");
    const el = $("discover-status");
    const labels = {
      idle: "Library index not built yet.",
      scanning: `Indexing the USB library… ${status.scanned} folders, ${status.total} tracks found.`,
      ready: `${status.total} tracks indexed. Search works offline.`,
      error: `Index error: ${status.error}`,
    };
    aiConfigured = Boolean(status.ai_configured);
    const aiRow = $("discover-ai-row");
    if (aiRow) aiRow.hidden = !aiConfigured;
    el.textContent = (labels[status.state] || "Library index unavailable.") +
      (aiConfigured ? " Optional AI wording is configured." : "");
    loadLibraryHealth();
    if (status.state === "scanning") setTimeout(loadIndexStatus, 2500);
  } catch (error) {
    $("discover-status").textContent = `Index unavailable: ${error.message}`;
  }
}

async function runDiscoverSearch() {
  const query = $("discover-query").value.trim();
  if (!query) return toast("Enter a search phrase", true);
  const button = $("discover-search");
  button.disabled = true;
  try {
    const useAi = $("discover-ai").checked ? 1 : 0;
    const body = await api(`/api/library/search?q=${encodeURIComponent(query)}&limit=500&ai=${useAi}`);
    discoverItems = body.items || [];
    renderDiscover();
    toast(`${body.total} match${body.total === 1 ? "" : "es"} · ${body.source} search`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderDiscover() {
  const list = $("discover-results");
  if (!discoverItems.length) {
    list.innerHTML = `<p class="muted">No matches. Try a shorter phrase.</p>`;
    return;
  }
  list.innerHTML = discoverItems.map((item, index) => `
    <div class="queue-row">
      <span class="qnum">${index + 1}</span>
      <span class="qtitle">${esc(item.title)}<small>${esc(item.artist || item.path)}</small></span>
      <span class="qalbum">${esc(item.album || "")}</span>
      <span class="row-actions">
        ${starHtml(isFavourite(item), `data-fav-discover="${index}"`)}
        <button class="row-btn" data-dplay="${index}" title="Play now">▶</button>
        <button class="row-btn menu-btn" data-item-menu="${index}" title="More actions" aria-label="More actions">⋮</button>
      </span>
    </div>`).join("");
  list.querySelectorAll("[data-dplay]").forEach((button) => button.addEventListener(
    "click", () => mediaCommand(discoverItems[Number(button.dataset.dplay)], "media_play")));
  list.querySelectorAll("[data-item-menu]").forEach((button) => button.addEventListener(
    "click", () => openItemMenu(button, discoverItems[Number(button.dataset.itemMenu)])));
  list.querySelectorAll("[data-fav-discover]").forEach((button) => button.addEventListener(
    "click", () => toggleFavourite(discoverItems[Number(button.dataset.favDiscover)])));
}

async function createSmartPlaylist(queryOverride = "", aiOverride = null) {
  const query = queryOverride || $("discover-query")?.value.trim() || "";
  if (!query) return toast("Enter a search phrase first", true);
  const suggested = prompt("Name for this playlist:", query.slice(0, 40));
  const name = (suggested || "").trim();
  if (!name) return;

  const wanted = name.toLocaleLowerCase();
  const duplicate = playlistRecords.find((record) =>
    String(record.name || "").trim().toLocaleLowerCase() === wanted);
  let mode = "create";
  let id = "";
  if (duplicate) {
    const choice = await askChoice(
      "Playlist name already used",
      `A playlist called "${name}" already exists. Merge keeps its tracks and adds new matches; overwrite replaces it.`,
      [
        { label: "Merge", value: "merge", primary: true },
        { label: "Overwrite", value: "overwrite" },
        { label: "Cancel", value: "cancel" },
      ],
    );
    if (choice === "cancel") return;
    mode = choice;
    id = duplicate.id;
  }

  try {
    const ai = aiOverride === null ? Boolean($("discover-ai")?.checked) : aiOverride;
    const body = await postJSON("/api/playlists/smart", { name, query, ai, mode, id });
    const resolved = await api(`/api/playlists/${encodeURIComponent(body.playlist.id)}`);
    playlist = {
      name,
      items: (resolved.playlist.items || []).map((item) => ({ ...item })),
    };
    currentPlaylistId = body.playlist.id;
    savePlaylist();
    await loadServerPlaylists();
    renderPlaylist();
    const verb = mode === "merge" ? "Merged into" :
      mode === "overwrite" ? "Overwrote" : "Created";
    toast(`${verb} "${name}" (${playlist.items.length} tracks)`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function reindexLibrary() {
  try {
    await postJSON("/api/library/reindex", {});
    toast("Library re-scan started");
    loadIndexStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadLibraryHealth() {
  const card = $("library-health");
  if (!card) return;
  try {
    const health = await api("/api/library/health");
    card.hidden = health.state === "idle";
    if (card.hidden) return;
    const title = $("library-health-title");
    const summary = $("library-health-summary");
    const detail = $("library-health-detail");
    if (health.state === "checking") {
      title.textContent = "Deep scan running…";
      summary.textContent = `Checked ${health.checked || 0} tracks in ${health.containers || 0} folders.`;
      detail.textContent = "Reading USB metadata through the player. This is read-only.";
      detail.className = "hint";
      setTimeout(loadLibraryHealth, 1500);
      return;
    }
    if (health.state === "error") {
      title.textContent = "Deep scan failed";
      summary.textContent = health.error || "The player could not be browsed.";
      detail.textContent = "";
      return;
    }
    title.textContent = "Library health";
    summary.textContent = `${health.checked || 0} tracks checked · ${health.containers || 0} folders · ${health.added_count || 0} new`;
    const problems = [];
    if (health.missing_count) problems.push(`${health.missing_count} missing`);
    if (health.stale_count) problems.push(`${health.stale_count} stale paths`);
    if (health.duration_count) problems.push(`${health.duration_count} without duration`);
    if (health.unreadable_count) problems.push(`${health.unreadable_count} unreadable folders`);
    detail.textContent = problems.length
      ? `${problems.join(" · ")}. Re-scan the library to refresh the index.`
      : "No broken, stale or unreadable entries found.";
    detail.className = "hint " + (problems.length ? "bad" : "good");
  } catch (error) {
    $("library-health-title").textContent = "Library health unavailable";
    $("library-health-summary").textContent = error.message;
  }
}

async function startDeepScan() {
  const button = $("library-deep-scan");
  button.disabled = true;
  try {
    await postJSON("/api/library/deep-scan", {});
    toast("Deep scan started");
    loadLibraryHealth();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function saveQueueAsPlaylist() {
  const items = queueItems();
  if (!items.length) return toast("The queue is empty", true);
  const name = prompt("Name for this playlist:", "Current queue");
  if (!name) return;
  try {
    await postJSON("/api/playlists", {
      playlist: { name, kind: "static", items: items.map((item) => ({ ...item })) },
    });
    await loadServerPlaylists();
    toast(`Saved ${items.length} queue items as \"${name}\"`);
  } catch (error) {
    toast(error.message, true);
  }
}

function addToPlaylist(item) {
  if (!item) return;
  if (playlist.items.some((existing) => existing.key === item.key)) {
    toast("Already in the playlist");
    return;
  }
  playlist.items.push({ ...item });
  savePlaylist();
  renderPlaylist();
  toast(`Added "${item.title}"`);
}

function queueItems() {
  return (state.queue?.items || []).map((item, index) => {
    const md = item.metadata || {};
    const position = Number(item.position ?? index);
    return {
      key: `q:${item.id ?? index}`,
      kind: "queue",
      id: item.id,
      index: position,
      title: md.title || md.name || "Unknown",
      artist: md.artist || "",
      album: md.album || "",
      source: md.source || "",
      duration: Number(md.duration || 0),
      class: md.class || "",
    };
  });
}

function presetItem(preset) {
  return {
    key: `p:${preset.id}`,
    kind: "preset",
    preset: Number(preset.id),
    title: preset.name || `Preset ${preset.id}`,
    artist: preset.type || "Radio",
    album: "",
    source: "Preset",
  };
}

async function refresh() {
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    state = await response.json();
    if (state.device_address) {
      controllerConfig.host = state.device_address;
      controllerConfig.port = Number(state.device_port || 80);
    }
    const powered = state.connected ? playerPowered() : false;
    if (lastPowered === false && powered && state.connected) {
      // The player woke up. USB roots and resource paths may have changed.
      libraryLoaded = false;
      if (view === "library") loadBrowse("0", "USB Drives", true);
    }
    lastPowered = powered;
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
    pendingNow = null;
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
  $("device-name").textContent = deviceLabel();
  if (state.connected && !splashManual) setSplash(false);
  const pill = $("conn-pill");
  const powered = playerPowered();
  const known = playerPowerKnown();
  if (!state.connected) {
    pill.className = "pill off";
    pill.innerHTML = "<i></i>Player offline";
    pill.title = "Click to change the player IP or port";
  } else if (!known) {
    pill.className = "pill";
    pill.innerHTML = "<i></i>Connecting…";
    pill.title = "Waiting for the player to report its power state";
  } else if (!powered) {
    pill.className = "pill standby";
    pill.innerHTML = "<i></i>Connected · Standby";
    pill.title = "Player is in standby. Choose music or press ⏻ to wake it. Click to change IP or port.";
  } else {
    pill.className = "pill on";
    pill.innerHTML = "<i></i>Connected · On";
    pill.title = "Player is on. Click to change the player IP or port.";
  }

  $("power-btn").classList.toggle("active", powered);
  $("power-btn").title = powered ? "Put player in standby" : "Wake the player";
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
  if (view === "library") renderLibrary();
  if (view === "playlist") renderPlaylist();
  if (view === "favourites") renderFavourites();
  if (view === "settings" && !$("view-settings").contains(document.activeElement)) renderSettings();
}

let miniMode = false;

function setMini(active) {
  miniMode = Boolean(active);
  document.body.classList.toggle("mini", miniMode);
  const player = $("mini-player");
  if (player) player.hidden = !miniMode;
  if (miniMode) document.title = "HifiMagic · Mini player";
  else document.title = "HifiMagic · StreamMagic player";
}

function openMiniWindow() {
  const url = `${location.pathname}?mini=1`;
  const popup = window.open(
    url, "hifimagic-mini",
    "popup=yes,width=420,height=180,menubar=no,toolbar=no,location=no,status=no",
  );
  if (!popup) {
    toast("Allow pop-ups for HifiMagic to open the mini player", true);
    return;
  }
  try { popup.focus(); } catch (error) { /* browser decides focus */ }
}

function renderMini() {
  const md = (state.playback || {}).metadata || {};
  const display = (state.now_playing || {}).display || {};
  const title = md.title || display.line1 || "Nothing playing";
  const artist = md.artist || display.line2 || deviceLabel();
  $("mini-title").textContent = title;
  $("mini-artist").textContent = artist;
  $("mini-track-title").textContent = title;
  $("mini-track-artist").textContent = artist;
  $("mini-play").textContent = (state.playback || {}).state === "play" ? "❚❚" : "▶";

  const cover = $("mini-cover");
  const art = currentArt();
  cover.hidden = !art;
  if (art && cover.getAttribute("src") !== art) cover.src = art;

  const zone = state.zone || {};
  const canVolume = zone.pre_amp_mode === true || (zone.cbus && zone.cbus !== "off");
  const volume = $("mini-volume");
  volume.disabled = !canVolume;
  volume.title = canVolume ? "Volume" : "Volume is controlled by the amplifier";
  if (canVolume && document.activeElement !== volume) {
    volume.value = zone.volume_percent ?? 0;
  }
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

function artistKey(name) {
  return String(name || "").trim().replace(/\s+/g, " ").toLocaleLowerCase();
}

function shortArtistLine(text, max = 180) {
  const clean = String(text || "").replace(/\s+/g, " ").trim();
  if (!clean) return "";
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max);
  const stops = [cut.lastIndexOf(". "), cut.lastIndexOf("! "), cut.lastIndexOf("? ")];
  const stop = Math.max(...stops);
  const shortened = stop > 60 ? cut.slice(0, stop + 1) : cut.replace(/\s+\S*$/, "");
  return `${shortened.trimEnd()}…`;
}

function localArtistLine(info) {
  if (!info) return "Local artist details are loading…";
  const bits = [];
  if ((info.genres || []).length) bits.push(info.genres.slice(0, 2).join(" / "));
  if (info.album_count) bits.push(`${info.album_count} album${info.album_count === 1 ? "" : "s"} here`);
  if (info.local_tracks) bits.push(`${info.local_tracks} track${info.local_tracks === 1 ? "" : "s"}`);
  return bits.length ? bits.join(" · ") : `${info.artist || "This artist"} is in your local library.`;
}

function paintArtistNote(name) {
  const line = $("artist-note-line");
  const toggle = $("artist-toggle");
  const panel = $("artist-panel");
  const body = $("artist-panel-body");
  const fetchButton = $("artist-fetch");
  const source = $("artist-source");
  if (!line || !toggle || !panel || !body || !fetchButton || !source) return;

  const info = artistInfo && artistKey(artistInfo.artist || artistInfoArtist) === artistInfoKey
    ? artistInfo : null;
  const summary = info?.bio?.summary || "";
  line.textContent = summary
    ? shortArtistLine(summary)
    : localArtistLine(info);

  toggle.hidden = !info;
  toggle.textContent = artistInfoExpanded ? "Show less" : "Expand";
  panel.hidden = !info || !artistInfoExpanded;

  if (!info) return;
  const facts = [];
  if ((info.genres || []).length) facts.push(`Genres: ${info.genres.slice(0, 5).join(", ")}`);
  if (info.album_count) facts.push(`${info.album_count} album${info.album_count === 1 ? "" : "s"} in your library`);
  if (info.local_tracks) facts.push(`${info.local_tracks} track${info.local_tracks === 1 ? "" : "s"}`);

  let html = "";
  if (summary) html += `<p>${esc(summary)}</p>`;
  else if (info.error) html += `<p>${esc(info.error)}</p>`;
  if (facts.length) {
    html += `<div class="artist-facts">${facts.map((fact) => `<span>${esc(fact)}</span>`).join("")}</div>`;
  }
  if ((info.albums || []).length) {
    html += `<div class="muted">Albums: ${esc(info.albums.slice(0, 6).join(", "))}</div>`;
  }
  if ((info.sample_tracks || []).length) {
    const titles = info.sample_tracks.map((track) => track.title).filter(Boolean).slice(0, 4);
    if (titles.length) html += `<div class="muted">Tracks: ${esc(titles.join(", "))}</div>`;
  }
  body.innerHTML = html || `<p>Local artist details are loading…</p>`;

  fetchButton.hidden = Boolean(info.bio);
  fetchButton.disabled = artistInfoFetching;
  fetchButton.textContent = artistInfoFetching ? "Fetching…" : "Fetch bio";
  if (info.bio?.url) {
    source.href = info.bio.url;
    source.hidden = false;
  } else {
    source.hidden = true;
  }
}

function renderArtistNote(name) {
  const note = $("artist-note");
  const panel = $("artist-panel");
  const artist = String(name || "").trim();
  if (!artist) {
    note.hidden = true;
    panel.hidden = true;
    return;
  }
  note.hidden = false;
  const key = artistKey(artist);
  if (key !== artistInfoKey) {
    artistInfoKey = key;
    artistInfoArtist = artist;
    artistInfo = null;
    artistInfoExpanded = false;
    artistInfoFetching = false;
    if (artistInfoPending !== key) {
      artistInfoPending = key;
      postJSON("/api/artist-info", { artist, fetch: false })
        .then((data) => {
          if (artistKey(artist) !== artistInfoKey) return;
          artistInfo = data;
          paintArtistNote(artist);
        })
        .catch(() => {
          if (artistKey(artist) !== artistInfoKey) return;
          artistInfo = { ok: true, artist, error: "Local artist details are unavailable." };
          paintArtistNote(artist);
        })
        .finally(() => {
          if (artistInfoPending === key) artistInfoPending = "";
        });
    }
  } else {
    artistInfoArtist = artist;
  }
  paintArtistNote(artist);
}

async function fetchArtistBio() {
  const artist = artistInfoArtist;
  if (!artist || artistInfoFetching) return;
  artistInfoFetching = true;
  paintArtistNote(artist);
  try {
    artistInfo = await postJSON("/api/artist-info", { artist, fetch: true });
    artistInfoExpanded = true;
    toast(artistInfo.error || "Artist bio added", Boolean(artistInfo.error));
  } catch (error) {
    toast(error.message || "Could not fetch artist bio", true);
  } finally {
    artistInfoFetching = false;
    paintArtistNote(artist);
  }
}

async function loadMusicDbSettings() {
  try {
    musicDb = await api("/api/music-db/settings");
  } catch (error) {
    toast(`Music database settings unavailable: ${error.message}`, true);
  }
  renderMusicDbSettings();
}

function renderMusicDbSettings() {
  const provider = $("musicdb-provider");
  const url = $("musicdb-url");
  const status = $("musicdb-status");
  if (provider) provider.value = musicDb.provider || "musicbrainz";
  if (url) url.value = musicDb.custom_url || "";
  if (status) {
    status.textContent = `Current source: ${musicDb.label || "MusicBrainz"}. ` +
      (musicDb.provider === "local"
        ? "No network lookup is used."
        : "The source is contacted only when you press Fetch album info.");
  }
}

async function saveMusicDbSettings() {
  const provider = $("musicdb-provider")?.value || "musicbrainz";
  const custom_url = $("musicdb-url")?.value || "";
  if (provider === "custom" && !custom_url && !advancedSettings) {
    setAdvancedSettings(true);
    toast("Advanced settings shown so you can enter the custom URL");
    return;
  }
  try {
    musicDb = await postJSON("/api/music-db/settings", { provider, custom_url });
    renderMusicDbSettings();
    toast(`Album metadata source: ${musicDb.label || provider}`);
  } catch (error) {
    toast(error.message, true);
    renderMusicDbSettings();
  }
}

function renderAlbumInfo(body) {
  albumInfo = body;
  const holder = $("album-info");
  if (!holder) return;
  const info = body?.info;
  const local = body?.local || {};
  holder.hidden = false;
  if (!info) {
    const tracks = Number(local.local_tracks || 0);
    holder.innerHTML = `<p class="muted">${esc(body?.error || "No album info found.")}</p>` +
      `<p class="hint">${tracks ? `${tracks} matching track${tracks === 1 ? "" : "s"} in your local library.` : "No matching tracks in your local library."}</p>`;
    return;
  }
  const facts = [];
  if (info.year) facts.push(info.year);
  if (info.genre) facts.push(info.genre);
  if (local.local_tracks) facts.push(`${local.local_tracks} local track${local.local_tracks === 1 ? "" : "s"}`);
  if (info.source) facts.push(info.source);
  const art = info.art_url
    ? `<img src="${esc(info.art_url)}" alt="">` : "";
  const summary = info.summary
    ? `<p>${esc(info.summary)}</p>`
    : `<p class="muted">${info.provider === "musicbrainz"
      ? "MusicBrainz provides release facts, not a written summary."
      : "This source did not provide a written summary."}</p>`;
  holder.innerHTML = `${art}<h4>${esc(info.album || body.album || "Album")}</h4>
    <p class="muted">${esc(info.artist || body.artist || "")}</p>
    ${summary}
    <div class="album-facts">${facts.filter(Boolean).map((fact) => `<span>${esc(fact)}</span>`).join("")}</div>
    ${info.url ? `<p><a class="text-link" href="${esc(info.url)}" target="_blank" rel="noopener noreferrer">View source ↗</a></p>` : ""}
    ${body.cached ? `<p class="hint">Cached locally.</p>` : ""}`;
}

async function fetchAlbumInfo() {
  const artist = ($("album-artist")?.value || "").trim();
  const album = ($("album-title")?.value || "").trim();
  if (!artist || !album) return toast("Enter an artist and album first", true);
  const button = $("album-fetch");
  const holder = $("album-info");
  button.disabled = true;
  if (holder) holder.innerHTML = `<p class="muted">Looking up album info…</p>`;
  try {
    const body = await postJSON("/api/album-info", { artist, album, fetch: true });
    renderAlbumInfo(body);
    toast(body.error || (body.fetched_now ? "Album info saved locally" : "Album info ready"), Boolean(body.error));
  } catch (error) {
    if (holder) holder.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function useNowPlayingForAlbum() {
  const md = (state.playback || {}).metadata || (state.now_playing || {}).metadata || {};
  const artist = String(md.artist || md.album_artist || "").trim();
  const album = String(md.album || "").trim();
  if (!artist || !album) return toast("Now playing has no artist and album", true);
  $("album-artist").value = artist;
  $("album-title").value = album;
  toast(`Filled in ${artist} — ${album}`);
}

function setPendingNow(title, subtitle = "", source = "") {
  pendingNow = {
    title: String(title || "").trim(),
    subtitle: String(subtitle || "").trim(),
    source: String(source || "").trim(),
    at: Date.now(),
  };
  renderNow();
}

function isInternetRadio() {
  const md = (state.playback || {}).metadata || {};
  const now = state.now_playing || {};
  const display = now.display || {};
  return String(md.class || "").startsWith("md.radio")
    || String(display.class || "").startsWith("stream.radio")
    || (now.source || {}).id === "IR"
    || String(md.source || "").toUpperCase() === "IR";
}

function renderNow() {
  const playback = state.playback || {};
  const md = playback.metadata || {};
  const now = state.now_playing || {};
  const display = now.display || {};
  const controls = new Set(now.controls || []);
  const isRadio = isInternetRadio();
  const deviceTitle = String(md.title || display.line1 || "").trim();
  if (pendingNow && deviceTitle && (
    deviceTitle === pendingNow.title
    || deviceTitle.includes(pendingNow.title)
    || pendingNow.title.includes(deviceTitle)
  )) pendingNow = null;
  if (pendingNow && Date.now() - pendingNow.at > 15000) pendingNow = null;

  let title = md.title || display.line1 || "Nothing playing";
  let artist = md.artist || display.line2 || "";
  let album = md.album || display.line3 || "";
  let sourceLabel = (now.source || {}).name || md.source || "—";
  if (isRadio) {
    const station = String(md.station || display.line2 || md.title || "").trim();
    const track = String(display.line1 || md.title || "").trim();
    if (station && track && station !== track) {
      title = station;
      artist = `Now playing: ${track}`;
    } else if (station) {
      title = station;
      artist = "Tuning in…";
    }
    album = "";
  }
  if (pendingNow) {
    if (pendingNow.title) title = pendingNow.title;
    if (pendingNow.subtitle) artist = pendingNow.subtitle;
    if (pendingNow.source) sourceLabel = pendingNow.source;
  }
  $("now-title").textContent = title;
  $("now-artist").textContent = artist;
  $("now-album").textContent = album;
  renderArtistNote(isRadio || pendingNow ? "" : artist);
  $("source-badge").textContent = sourceLabel;
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
  const sources = Array.isArray(state.sources) ? state.sources : (state.sources?.sources || []);
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
  const kind = String(preset.type || preset.class || "Stream").split(" ")[0];
  const art = String(preset.art_url || preset.image_url ||
    (preset.metadata || {}).art_url || "").trim();
  const badge = esc(String(preset.name || "?").trim().charAt(0).toUpperCase() || "?");
  return `<div class="preset-card ${preset.is_playing ? "playing" : ""}">
      <div class="preset-art-wrap">
        ${art ? `<img class="preset-art" src="${esc(art)}" alt=""
          onerror="this.hidden=true;this.nextElementSibling.hidden=false">` : ""}
        <span class="preset-art-fallback" ${art ? "hidden" : ""} aria-hidden="true">${badge}</span>
        <span class="preset-number" aria-hidden="true">${esc(preset.id)}</span>
        <button class="play-preset" data-play="${esc(preset.id)}" title="Play ${esc(preset.name)}">▶</button>
      </div>
      <div class="preset-body">
        <strong title="${esc(preset.name)}">${esc(preset.name)}</strong>
        <small>${esc(kind)} · preset ${esc(preset.id)}</small>
      </div>
      <div class="preset-actions">
        <button data-addpreset="${esc(preset.id)}" title="Add to playlist">Add</button>
        <button data-rename="${esc(preset.id)}" title="Rename">Rename</button>
        <button data-delete="${esc(preset.id)}" title="Delete">Delete</button>
      </div>
    </div>`;
}

function bindPresetCards(container) {
  container.querySelectorAll("[data-play]").forEach((button) => button.addEventListener("click", () => {
    const preset = (state.presets?.presets || []).find(
      (item) => Number(item.id) === Number(button.dataset.play)
    );
    if (preset) setPendingNow(preset.name || "Internet Radio", "Tuning in…", "Internet Radio");
    act("preset", { preset: Number(button.dataset.play) });
  }));
  container.querySelectorAll("[data-addpreset]").forEach((button) => button.addEventListener("click", () => {
    const preset = (state.presets?.presets || []).find((p) => Number(p.id) === Number(button.dataset.addpreset));
    if (preset) addToPlaylist(presetItem(preset));
  }));
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

function renderQueue() {
  const queue = state.queue || {};
  const items = queue.items || [];
  const current = queue.play_postition ?? (state.playback || {}).queue_index ?? -1;
  $("queue-summary").textContent = `${queue.total ?? items.length} item${items.length === 1 ? "" : "s"}`;
  $("queue-list").innerHTML = items.length ? items.map((item) => {
    const md = item.metadata || {};
    const position = Number(item.position ?? 0);
    const canEdit = capability("queue_edit") && capability("queue_move");
    return `<div class="queue-row ${position === current ? "current" : ""}" data-queue-id="${esc(item.id ?? "")}">
        <span class="qnum">${position === current ? "▶" : position + 1}</span>
        <span class="qtitle">${esc(md.title || md.name || "Untitled track")}<small>${esc(md.artist || "")}</small></span>
        <span class="qalbum">${esc(md.album || "")}</span>
        <span class="row-actions">
          ${starHtml(isFavourite(libraryItemForMetadata(md) || {
            title: md.title || md.name, artist: md.artist, album: md.album, source: "Queue",
          }), `data-fav-queue="${esc(position)}"`)}
          <button class="row-btn" data-addqueue="${esc(item.id ?? position)}">+ List</button>
          ${canEdit ? `<button class="row-btn" data-queue-move="${esc(position)}" data-dir="-1" title="Move up" ${position === 0 ? "disabled" : ""}>↑</button>
          <button class="row-btn" data-queue-move="${esc(position)}" data-dir="1" title="Move down" ${position >= (queue.total ?? items.length) - 1 ? "disabled" : ""}>↓</button>
          <button class="row-btn" data-queue-remove="${esc(item.id ?? "")}" title="Remove from queue">✕</button>` : ""}
          <button class="row-btn" data-index="${esc(position)}">Play</button>
        </span>
      </div>`;
  }).join("") : `<p class="muted">The queue is empty.</p>`;
  $("queue-list").querySelectorAll("[data-index]").forEach((button) => button.addEventListener("click", () => {
    const position = Number(button.dataset.index);
    const track = (queue.items || []).find((item) => Number(item.position) === position);
    const md = track?.metadata || {};
    setPendingNow(md.title || md.name || "Playing track",
      md.artist || "Starting playback", "Media Library");
    act("queue_index", { index: position });
  }));
  $("queue-list").querySelectorAll("[data-addqueue]").forEach((button) => button.addEventListener("click", () => {
    const item = queueItems().find((track) => String(track.id) === button.dataset.addqueue);
    if (item) addToPlaylist(item);
  }));
  $("queue-list").querySelectorAll("[data-fav-queue]").forEach((button) => button.addEventListener("click", () => {
    const entry = (queue.items || []).find((item) => Number(item.position) === Number(button.dataset.favQueue));
    if (!entry) return;
    const md = entry.metadata || {};
    toggleFavourite(libraryItemForMetadata(md) || {
      title: md.title || md.name, artist: md.artist, album: md.album, source: "Queue",
    });
  }));
  $("queue-list").querySelectorAll("[data-queue-move]").forEach((button) => button.addEventListener("click", () => {
    const from = Number(button.dataset.queueMove);
    const to = from + Number(button.dataset.dir);
    const entry = (queue.items || []).find((item) => Number(item.position) === from);
    if (!entry || to < 0 || to >= (queue.total ?? items.length)) return;
    act("queue_move", { id: Number(entry.id), from, to });
  }));
  $("queue-list").querySelectorAll("[data-queue-remove]").forEach((button) => button.addEventListener("click", () => {
    if (!confirm("Remove this track from the player's queue?")) return;
    act("queue_remove", { id: Number(button.dataset.queueRemove) });
  }));
}

function isMediaPlayable(item) {
  const kind = String(item?.class || "");
  return kind.startsWith("object.item.audioItem") || kind.startsWith("object.container");
}

function mediaPayload(item) {
  return {
    id: item.id,
    parent_id: item.parent_id,
    class: item.class,
    title: item.title,
    artist: item.artist,
    album: item.album,
    track: item.track,
    duration: item.duration,
    resource: item.resource,
    art_url: item.art_url,
    is_container: item.is_container,
    didl: item.didl || "",
  };
}

function mediaPlaylistItem(item) {
  return {
    key: `u:${item.id}`,
    kind: "media",
    id: item.id,
    parent_id: item.parent_id,
    title: item.title,
    artist: item.artist || "",
    album: item.album || "",
    source: item.is_container ? "USB album" : "USB track",
    class: item.class || "object.item.audioItem.musicTrack",
    didl: item.didl || "",
    duration: Number(item.duration || 0),
    art_url: item.art_url || "",
    resource: item.resource || "",
  };
}

async function mediaCommand(item, action) {
  const messages = {
    media_play: `Playing ${item.title}`,
    media_play_next: `Next: ${item.title}`,
    media_add: `Added ${item.title} to queue`,
  };
  if (action === "media_play") {
    setPendingNow(item.title || "Playing track",
      item.artist || "Starting playback", "Media Library");
  }
  return act(action, { item: mediaPayload(item) }, messages[action] || "Done");
}

async function loadBrowse(objectId, title, reset = false, append = false) {
  if (browseLoading) return;
  browseLoading = true;
  const list = $("library-list");
  if (!append) list.innerHTML = `<p class="muted">Reading the USB library…</p>`;
  try {
    const start = append ? browseMeta.next_start : 0;
    const url = `/api/browse?object=${encodeURIComponent(objectId)}&start=${start}&count=100`;
    const response = await fetch(url, { cache: "no-store" });
    const body = await response.json();
    if (!response.ok || body.error) throw new Error(body.error || "Could not browse the USB library");
    if (reset) browseStack = [{ id: objectId, title }];
    browseItems = append ? browseItems.concat(body.items || []) : (body.items || []);
    browseMeta = {
      total: Number(body.total || 0),
      next_start: Number(body.next_start || browseItems.length),
      has_more: Boolean(body.has_more),
    };
    libraryLoaded = true;
    renderLibrary();
  } catch (error) {
    toast(error.message, true);
    list.innerHTML = `<p class="muted">${esc(error.message)}</p>`;
  } finally {
    browseLoading = false;
  }
}

function browseOpen(item) {
  browseStack.push({ id: item.id, title: item.title });
  loadBrowse(item.id, item.title);
}

async function refreshUsb() {
  const ok = await act("library_refresh", {}, "USB library refreshed");
  if (!ok) return;
  libraryLoaded = false;
  browseLoading = false;
  loadBrowse("0", "USB Drives", true);
}

function renderLibrary() {
  const current = browseStack.at(-1) || { id: "0", title: "USB Drives" };
  const term = ($("library-search").value || "").toLowerCase().trim();
  const visible = browseItems.filter((item) => !term ||
    [item.title, item.artist, item.album].some((value) => String(value || "").toLowerCase().includes(term)));
  $("library-path").textContent = browseStack.map((entry) => entry.title).join(" / ");
  $("library-up").disabled = browseStack.length <= 1;
  const standby = state.connected && playerPowerKnown() && !playerPowered();
  $("library-summary").textContent = standby
    ? "Player is in standby — browsing will wake it."
    : (browseMeta.total
      ? `${browseMeta.total} item${browseMeta.total === 1 ? "" : "s"} in this folder`
      : "USB library not loaded yet.");
  $("library-note").textContent = standby
    ? "The USB drive is not visible until the player wakes. Opening a folder, playing a track, or pressing Refresh USB will wake it (Network standby required)."
    : LIBRARY_NOTE;
  $("library-more-wrap").hidden = !browseMeta.has_more || Boolean(term);

  const list = $("library-list");
  list.classList.toggle("library-grid", libraryView === "icons");
  list.classList.toggle("queue-list", libraryView !== "icons");
  if (!visible.length) {
    list.innerHTML = libraryLoaded
      ? `<p class="muted">Nothing here matches the filter.</p>`
      : `<p class="muted">Choose a USB drive to browse its folders.</p>`;
    return;
  }

  const subtitleFor = (item) => item.is_container
    ? (item.class === "object.container.album.musicAlbum" ? "Album" : "Folder")
    : [item.artist, item.album].filter(Boolean).join(" · ");

  if (libraryView === "icons") {
    list.innerHTML = visible.map((item, index) => {
      const art = String(item.art_url || "").trim();
      const playable = isMediaPlayable(item);
      const badge = esc(String(item.title || "?").trim().charAt(0).toUpperCase() || "?");
      return `<div class="preset-card library-card" data-library-index="${index}">
          <div class="preset-art-wrap">
            ${art ? `<img class="preset-art" src="${esc(art)}" alt="" onerror="this.hidden=true;this.nextElementSibling.hidden=false">` : ""}
            <span class="preset-art-fallback" ${art ? "hidden" : ""} aria-hidden="true">${item.is_container ? "▤" : badge}</span>
            ${playable
              ? `<button class="play-preset" data-play="${index}" title="Play now">▶</button>`
              : `<button class="play-preset open-preset" data-open="${index}" title="Open folder">▤</button>`}
          </div>
          <div class="preset-body">
            <strong title="${esc(item.title)}">${esc(item.title)}</strong>
            <small>${esc(subtitleFor(item) || (item.is_container ? "Folder" : "Track"))}</small>
          </div>
          <div class="preset-actions">
            ${item.is_container ? `<button data-open="${index}">Open</button>` : ""}
            ${starHtml(isFavourite(item), `data-fav-library="${index}"`)}
            <button data-item-menu="${index}" title="More actions">⋮ More</button>
          </div>
        </div>`;
    }).join("");
    list.querySelectorAll("[data-library-index]").forEach((card) => card.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      const item = visible[Number(card.dataset.libraryIndex)];
      if (item.is_container) browseOpen(item);
      else if (isMediaPlayable(item)) mediaCommand(item, "media_play");
    }));
  } else {
    list.innerHTML = visible.map((item, index) => {
      const art = item.art_url
        ? `<img class="lib-art" src="${esc(item.art_url)}" alt="">`
        : `<span class="lib-icon">${item.is_container ? "▤" : "♪"}</span>`;
      const subtitle = subtitleFor(item);
      const actions = [];
      if (item.is_container) actions.push(`<button class="row-btn" data-open="${index}">Open</button>`);
      if (isMediaPlayable(item)) {
        actions.push(`<button class="row-btn" data-play="${index}" title="Play now">▶</button>`);
      }
      actions.push(starHtml(isFavourite(item), `data-fav-library="${index}"`));
      actions.push(`<button class="row-btn menu-btn" data-item-menu="${index}" title="More actions" aria-label="More actions">⋮</button>`);
      return `<div class="queue-row library-item">
          <span class="qnum">${art}</span>
          <span class="qtitle">${esc(item.title)}<small>${esc(subtitle)}</small></span>
          <span class="qalbum">${esc(item.is_container ? subtitle : item.album || "")}</span>
          <span class="row-actions">${actions.join("")}</span>
        </div>`;
    }).join("");
  }

  list.querySelectorAll("[data-open]").forEach((button) => button.addEventListener("click", () =>
    browseOpen(visible[Number(button.dataset.open)])));
  list.querySelectorAll("[data-play]").forEach((button) => button.addEventListener("click", () =>
    mediaCommand(visible[Number(button.dataset.play)], "media_play")));
  list.querySelectorAll("[data-item-menu]").forEach((button) => button.addEventListener("click", () =>
    openItemMenu(button, visible[Number(button.dataset.itemMenu)])));
  list.querySelectorAll("[data-fav-library]").forEach((button) => button.addEventListener("click", () =>
    toggleFavourite(visible[Number(button.dataset.favLibrary)])));
}

function setLibraryView(mode) {
  libraryView = mode === "list" ? "list" : "icons";
  try { localStorage.setItem("hifimagic.library-view", libraryView); } catch (_) { /* private mode */ }
  $("library-view-icons").classList.toggle("active", libraryView === "icons");
  $("library-view-list").classList.toggle("active", libraryView === "list");
  renderLibrary();
}

function renderPlaylist() {
  const nameInput = $("playlist-name");
  if (document.activeElement !== nameInput) nameInput.value = playlist.name;
  const items = playlist.items;
  $("playlist-count").textContent = `${items.length} item${items.length === 1 ? "" : "s"}`;
  const list = $("playlist-list");
  if (!items.length) {
    list.innerHTML = `<p class="muted">Playlist is empty. Add tracks from the Queue, USB / Library, Presets or Radio pages.</p>`;
    return;
  }
  list.innerHTML = items.map((item, index) => `
    <div class="queue-row playlist-row ${item.index === (state.queue || {}).play_postition && item.kind === "queue" ? "current" : ""}">
      <span class="qnum">${index + 1}</span>
      <span class="qtitle">${esc(item.title)}<small>${esc(item.artist || item.source || "")}</small></span>
      <span class="qalbum">${esc(item.album || item.source || "")}</span>
      <span class="row-actions">
        ${starHtml(isFavourite(item), `data-fav-playlist="${index}"`)}
        <button class="row-btn" data-move="${index}" data-dir="-1" title="Move up">↑</button>
        <button class="row-btn" data-move="${index}" data-dir="1" title="Move down">↓</button>
        <button class="row-btn" data-remove="${index}" title="Remove">✕</button>
      </span>
    </div>`).join("");

  list.querySelectorAll("[data-move]").forEach((button) => button.addEventListener("click", () => {
    const from = Number(button.dataset.move);
    const to = from + Number(button.dataset.dir);
    if (to < 0 || to >= items.length) return;
    [items[from], items[to]] = [items[to], items[from]];
    savePlaylist();
    renderPlaylist();
  }));
  list.querySelectorAll("[data-remove]").forEach((button) => button.addEventListener("click", () => {
    playlist.items.splice(Number(button.dataset.remove), 1);
    savePlaylist();
    renderPlaylist();
  }));
  list.querySelectorAll("[data-fav-playlist]").forEach((button) => button.addEventListener("click", () =>
    toggleFavourite(items[Number(button.dataset.favPlaylist)])));
}

function renderFavourites() {
  const list = $("favourites-list");
  if (!list) return;
  if (!favourites.length) {
    list.innerHTML = `<p class="muted">No favourites yet. Use ☆ on a track, album, queue entry or search result.</p>`;
    return;
  }
  list.innerHTML = favourites.map((item, index) => `
    <div class="queue-row">
      <span class="qnum">${item.kind === "album" ? "▤" : "♪"}</span>
      <span class="qtitle">${esc(item.title || "Untitled")}<small>${esc(item.artist || "")}</small></span>
      <span class="qalbum">${esc(item.album || item.source || "")}</span>
      <span class="row-actions">
        <button class="row-btn" data-fav-play="${index}" title="Play now">▶</button>
        <button class="row-btn" data-fav-list="${index}" title="Add to working playlist">+ List</button>
        <button class="row-btn" data-fav-remove="${esc(item.key)}" title="Remove favourite">★</button>
      </span>
    </div>`).join("");
  list.querySelectorAll("[data-fav-play]").forEach((button) => button.addEventListener(
    "click", () => playFavourite(favourites[Number(button.dataset.favPlay)])));
  list.querySelectorAll("[data-fav-list]").forEach((button) => button.addEventListener("click", () => {
    const item = favourites[Number(button.dataset.favList)];
    playlist.items.push(favouriteToPlaylistItem(item));
    savePlaylist();
    renderPlaylist();
    toast(`Added ${item.title || "item"} to the working playlist`);
  }));
  list.querySelectorAll("[data-fav-remove]").forEach((button) => button.addEventListener("click", () => {
    const item = favourites.find((entry) => entry.key === button.dataset.favRemove);
    if (item) toggleFavourite(item);
  }));
}

async function playFavourite(item) {
  if (!item?.id) return toast("This favourite has no playable media id", true);
  const payload = favouriteToPlaylistItem(item);
  setPendingNow(payload.title || "Playing favourite", payload.artist || "Starting playback", payload.source || "USB library");
  try {
    const body = await postJSON("/api/playlist/play", { items: [payload] });
    toast(body.queued ? `Playing ${payload.title || "track"}` : "The player could not play this item");
    await refresh();
  } catch (error) {
    pendingNow = null;
    toast(error.message, true);
  }
}

async function playPlaylist() {
  if (!playlist.items.length) return toast("Playlist is empty", true);
  const button = $("playlist-play");
  const first = playlist.items[0];
  setPendingNow(
    first.title || "Playing playlist",
    first.artist || "Starting playlist",
    "Media Library",
  );
  button.disabled = true;
  try {
    const body = await postJSON("/api/playlist/play", { items: playlist.items });
    const skipped = body.skipped || [];
    toast(skipped.length
      ? `Playing ${body.queued} track(s) · skipped ${skipped.length} non-library item(s)`
      : `Playing ${body.queued} track(s)`);
    await refresh();
  } catch (error) {
    pendingNow = null;
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
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
  const caps = state.capabilities || {};
  const capsKnown = Object.keys(caps).length > 0;
  const preAmpWritable = capability("pre_amp");
  const balanceWritable = capability("balance");
  const outputs = capability("outputs") ? (state.outputs?.outputs || []) : [];

  const showCard = (id, supported) => {
    const card = $(id);
    if (card) card.hidden = !supported;
  };
  const note = $("player-settings-note");
  if (note) note.textContent = capsKnown
    ? `Showing controls confirmed by ${deviceLabel()} (API ${device.api || "?"}).`
    : "Waiting for the player to report its capabilities…";
  if (capsKnown) {
    showCard("card-volume", capability("audio") || capability("zone"));
    showCard("card-eq", capability("eq") || capability("tilt"));
    showCard("card-display", capability("display"));
    showCard("card-power", capability("power"));
    showCard("card-sleep", capability("sleep_timer"));
    showCard("card-device", capability("device_info"));
    showCard("card-network", capability("network"));
  }

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

  const sleep = state.sleep_timer || {};
  const sleepActive = sleep.active === true;
  const sleepMinutes = Math.max(0, Math.ceil(Number(sleep.remaining || 0) / 60));
  const sleepHolder = $("sleep-settings");
  if (sleepHolder) sleepHolder.innerHTML = `
    <p class="hint">${sleepActive
      ? `Sleep timer active — about ${sleepMinutes} minute${sleepMinutes === 1 ? "" : "s"} left.`
      : "No sleep timer is active."}</p>
    <div class="setting-row"><label>Minutes</label>
      <select id="sleep-minutes">${[5, 10, 15, 20, 30, 45, 60, 90, 120]
        .map((value) => `<option value="${value}" ${value === 30 ? "selected" : ""}>${value}</option>`).join("")}</select>
    </div>
    <div class="playlist-actions">
      <button class="row-btn" id="sleep-start">Start timer</button>
      <button class="row-btn" id="sleep-cancel" ${sleepActive ? "" : "disabled"}>Cancel</button>
    </div>
    <p class="hint">Handled by the player. It keeps running even if this browser tab is closed.</p>`;

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
  if ($("sleep-start")) $("sleep-start").addEventListener("click", () => {
    act("sleep_timer", { minutes: Number($("sleep-minutes").value) }, "Sleep timer started on the player");
  });
  if ($("sleep-cancel")) $("sleep-cancel").addEventListener("click", () =>
    act("sleep_timer_cancel", {}, "Sleep timer cancelled"));
  $("backup-export").addEventListener("click", exportBackup);
  $("backup-import").addEventListener("click", () => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".zip,application/zip";
    input.addEventListener("change", () => importBackup(input.files[0]));
    input.click();
  });
}

/* ------------------------------------------------------------------ startup */

function showView(name) {
  const button = document.querySelector(`.nav-item[data-view="${name}"]`);
  if (!button) return;
  view = name;
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("active", section.id === "view-" + view));
  if (view === "library" && !libraryLoaded) loadBrowse("0", "USB Drives", true);
  if (view === "discover") loadIndexStatus();
  if (view === "favourites") loadFavourites();
  if (view === "radio") loadRadioPage();
  render();
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  showView(button.dataset.view);
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
}));
window.addEventListener("hashchange", () => showView(location.hash.replace(/^#/, "") || "now"));

$("cmd-open").addEventListener("click", openCommandPalette);
$("mini-launch").addEventListener("click", openMiniWindow);
$("mini-toggle").addEventListener("click", () => setMini(true));
$("mini-expand").addEventListener("click", () => {
  if (window.opener) {
    try { window.opener.focus(); } catch (error) { /* ignore */ }
    window.close();
  } else {
    openMiniWindow();
  }
});
$("mini-popout").addEventListener("click", openMiniWindow);
$("mini-prev").addEventListener("click", () => act("previous"));
$("mini-play").addEventListener("click", () => act("toggle"));
$("mini-next").addEventListener("click", () => act("next"));
$("mini-volume").addEventListener("change", (event) => act("volume_set", { value: Number(event.target.value) }));

$("conn-pill").addEventListener("click", () => {
  splashManual = true;
  $("connect-host").value = controllerConfig.host || $("connect-host").value;
  $("connect-port").value = controllerConfig.port || 80;
  setSplash(true, "Change the player address and click Connect.");
  $("connect-host").focus();
});
$("connect-form").addEventListener("submit", connectFromForm);
$("discover-button").addEventListener("click", findPlayers);
$("cmd-input").addEventListener("input", () => {
  clearTimeout(cmdTimer);
  cmdTimer = setTimeout(refreshCommandPalette, 180);
});
$("cmd-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    runCommandResult(0);
  }
});
$("cmd-overlay").addEventListener("click", (event) => {
  if (event.target === $("cmd-overlay")) closeCommandPalette();
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openCommandPalette();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "m") {
    event.preventDefault();
    setMini(!miniMode);
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "m") {
    event.preventDefault();
    openMiniWindow();
    return;
  }
  if (event.key === "Escape" && !$("cmd-overlay").hidden) {
    closeCommandPalette();
    return;
  }
  if (event.key === "Escape" && state.connected && !$("connect-button").disabled) {
    splashManual = false;
    setSplash(false);
  }
});

$("artist-toggle").addEventListener("click", () => {
  artistInfoExpanded = !artistInfoExpanded;
  paintArtistNote(artistInfoArtist);
});
$("artist-fetch").addEventListener("click", fetchArtistBio);
$("advanced-toggle").addEventListener("click", () => setAdvancedSettings(!advancedSettings));
$("musicdb-provider").addEventListener("change", saveMusicDbSettings);
$("musicdb-url").addEventListener("change", saveMusicDbSettings);
$("album-fetch").addEventListener("click", fetchAlbumInfo);
$("album-now").addEventListener("click", useNowPlayingForAlbum);

$("play-btn").addEventListener("click", () => act("toggle"));
$("prev-btn").addEventListener("click", () => act("previous"));
$("next-btn").addEventListener("click", () => act("next"));
$("stop-btn").addEventListener("click", () => act("stop"));
$("shuffle-btn").addEventListener("click", () => act("shuffle", { value: "toggle" }));
$("repeat-btn").addEventListener("click", () => act("repeat", { value: "toggle" }));
$("power-btn").addEventListener("click", () => {
  act(playerPowered() ? "standby" : "power_on");
});
$("preset-search").addEventListener("input", renderPresets);
$("library-search").addEventListener("input", renderLibrary);
$("library-add-all").addEventListener("click", () => {
  const items = browseItems.filter(isMediaPlayable);
  if (!items.length) return toast("Open a folder or album first", true);
  let added = 0;
  for (const item of items) {
    const entry = mediaPlaylistItem(item);
    if (playlist.items.some((existing) => existing.key === entry.key)) continue;
    playlist.items.push(entry);
    added += 1;
  }
  savePlaylist();
  renderPlaylist();
  toast(added ? `Added ${added} item(s)` : "All items are already in the playlist");
});
$("library-home").addEventListener("click", () => loadBrowse("0", "USB Drives", true));
$("library-refresh").addEventListener("click", refreshUsb);
$("library-up").addEventListener("click", () => {
  if (browseStack.length <= 1) return;
  browseStack.pop();
  const parent = browseStack.at(-1);
  loadBrowse(parent.id, parent.title);
});
$("library-more").addEventListener("click", () => {
  const current = browseStack.at(-1) || { id: "0", title: "USB Drives" };
  loadBrowse(current.id, current.title, false, true);
});
$("queue-save").addEventListener("click", saveQueueAsPlaylist);
$("queue-clear").addEventListener("click", () => {
  const total = Number((state.queue || {}).total || 0);
  if (!total || confirm(`Clear ${total} track${total === 1 ? "" : "s"} from the player queue?`)) {
    act("queue_clear", {}, "Queue cleared");
  }
});
$("playlist-play").addEventListener("click", playPlaylist);
$("playlist-new").addEventListener("click", newPlaylist);
$("playlist-save").addEventListener("click", saveNamedPlaylist);
$("playlist-smart").addEventListener("click", () =>
  createSmartPlaylist($("playlist-smart-query").value.trim(), $("discover-ai").checked));
$("playlist-smart-query").addEventListener("keydown", (event) => {
  if (event.key === "Enter") createSmartPlaylist($("playlist-smart-query").value.trim(), $("discover-ai").checked);
});
$("playlist-delete").addEventListener("click", deleteNamedPlaylist);
$("playlist-select").addEventListener("change", (event) => loadNamedPlaylist(event.target.value));
$("discover-search").addEventListener("click", runDiscoverSearch);
$("discover-query").addEventListener("keydown", (event) => {
  if (event.key === "Enter") runDiscoverSearch();
});
$("discover-smart").addEventListener("click", createSmartPlaylist);
$("discover-reindex").addEventListener("click", reindexLibrary);
$("library-deep-scan").addEventListener("click", startDeepScan);
$("library-view-icons").addEventListener("click", () => setLibraryView("icons"));
$("library-view-list").addEventListener("click", () => setLibraryView("list"));
$("playlist-clear").addEventListener("click", () => {
  if (!playlist.items.length || confirm("Clear the playlist?")) {
    playlist.items = [];
    savePlaylist();
    renderPlaylist();
  }
});
$("playlist-name").addEventListener("change", (event) => {
  playlist.name = event.target.value.trim() || "My playlist";
  savePlaylist();
  renderPlaylistSelect();
  renderPlaylist();
});
/* ---------------------------------------------------------- station directory */

function stationSaved(station) {
  return stationFavourites.some((item) => item.uuid === station.uuid);
}

function stationCard(station) {
  const art = String(station.favicon || "").trim();
  const saved = stationSaved(station);
  const detail = [
    station.country || station.countrycode, station.language, station.codec,
    station.bitrate ? `${station.bitrate} kbps` : "",
  ].filter(Boolean).join(" · ");
  const tags = String(station.tags || "").split(",").map((tag) => tag.trim()).filter(Boolean).slice(0, 4).join(", ");
  return `<div class="preset-card station-card ${saved ? "saved" : ""}" data-station-id="${esc(station.uuid)}" data-saved="${saved}">
      <div class="preset-art-wrap">
        ${art ? `<img class="preset-art" src="${esc(art)}" alt="" onerror="this.hidden=true;this.nextElementSibling.hidden=false">` : ""}
        <span class="preset-art-fallback" ${art ? "hidden" : ""} aria-hidden="true">◉</span>
        <button class="play-preset" data-station-play title="Play ${esc(station.name)}">▶</button>
      </div>
      <div class="preset-body">
        <strong title="${esc(station.name)}">${esc(station.name)}</strong>
        <small title="${esc([tags, detail].filter(Boolean).join(" · "))}">${esc(detail || tags || "Internet radio")}</small>
      </div>
      <div class="preset-actions">
        <button class="save-station" data-station-save title="${saved ? "Remove from saved stations" : "Save this station"}">${saved ? "★ Saved" : "☆ Save"}</button>
        <button data-station-add title="Add to playlist">Add</button>
      </div>
    </div>`;
}

function bindStationCards(container, stations) {
  container.querySelectorAll("[data-station-id]").forEach((card) => {
    const station = stations.find((item) => item.uuid === card.dataset.stationId);
    if (!station) return;
    const play = card.querySelector("[data-station-play]");
    if (play) play.addEventListener("click", () => {
      setPendingNow(station.name || "Internet Radio", "Tuning in…", "Internet Radio");
      act("radio_url", { url: station.url, name: station.name }, `Playing ${station.name}`);
    });
    const save = card.querySelector("[data-station-save]");
    if (save) save.addEventListener("click", () => toggleStationFavourite(station));
    const add = card.querySelector("[data-station-add]");
    if (add) add.addEventListener("click", () => addToPlaylist({
      key: `u:${station.url}`, kind: "url", title: station.name,
      artist: station.tags || "Internet Radio", album: station.country || "",
      source: "Station directory", url: station.url,
    }));
  });
}

function renderStationGrid() {
  const grid = $("station-grid");
  if (!stationResults.length) {
    grid.innerHTML = `<p class="muted">Choose filters and select Find stations.</p>`;
    return;
  }
  grid.innerHTML = stationResults.map(stationCard).join("");
  bindStationCards(grid, stationResults);
}

function renderStationFavourites() {
  const container = $("station-favourites");
  if (!stationFavourites.length) {
    container.innerHTML = `<p class="muted">No saved stations yet. Save one from the directory above.</p>`;
    return;
  }
  container.innerHTML = stationFavourites.map((station, index) => `
    <div class="queue-row">
      <span class="qnum">${index + 1}</span>
      <span class="qtitle">${esc(station.name)}<small>${esc(station.tags || station.country || "Internet radio")}</small></span>
      <span class="qalbum">${esc(station.bitrate ? `${station.bitrate} kbps` : station.codec || "")}</span>
      <span class="row-actions">
        <button data-fav-play="${index}" title="Play station">▶</button>
        <button data-fav-remove="${index}" title="Remove saved station">✕</button>
      </span>
    </div>`).join("");
  container.querySelectorAll("[data-fav-play]").forEach((button) => button.addEventListener("click", () => {
    const station = stationFavourites[Number(button.dataset.favPlay)];
    if (!station) return;
    setPendingNow(station.name || "Internet Radio", "Tuning in…", "Internet Radio");
    act("radio_url", { url: station.url, name: station.name }, `Playing ${station.name}`);
  }));
  container.querySelectorAll("[data-fav-remove]").forEach((button) => button.addEventListener("click", () => {
    const station = stationFavourites[Number(button.dataset.favRemove)];
    if (station) toggleStationFavourite(station);
  }));
}

async function loadStationFilters() {
  if (stationFilters) return;
  try {
    const data = await api("/api/radio/filters");
    stationFilters = data;
    const country = $("station-country");
    const tag = $("station-tag");
    for (const item of data.countries || []) country.add(new Option(item.name, item.code));
    for (const item of data.tags || []) tag.add(new Option(`${item.name} (${item.stations})`, item.name));
    $("station-status").textContent =
      `Directory ready · ${(data.countries || []).length} countries, ${(data.tags || []).length} genres.`;
  } catch (error) {
    $("station-status").textContent = `Directory unavailable: ${error.message}`;
  }
}

async function loadStationFavourites() {
  try {
    const data = await api("/api/radio/favourites");
    stationFavourites = data.items || [];
    renderStationFavourites();
    renderStationGrid();
  } catch (error) {
    $("station-favourites").innerHTML = `<p class="muted">Saved stations unavailable: ${esc(error.message)}</p>`;
  }
}

async function loadRadioPage() {
  if (radioPageLoaded) return;
  radioPageLoaded = true;
  renderStationGrid();
  await Promise.allSettled([loadStationFilters(), loadStationFavourites()]);
}

async function runStationSearch(refresh = false) {
  if (stationLoading) return;
  stationLoading = true;
  $("station-status").textContent = "Searching the station directory…";
  try {
    const data = await postJSON("/api/radio/search", {
      query: $("station-query").value.trim(),
      country: $("station-country").value,
      tag: $("station-tag").value,
      language: $("station-language").value,
      bitrate_min: Number($("station-bitrate").value || 0),
      order: $("station-order").value,
      limit: 24,
      refresh,
    });
    stationResults = data.items || [];
    renderStationGrid();
    if (data.error) toast(data.error, true);
    $("station-status").textContent = stationResults.length
      ? `${stationResults.length} station${stationResults.length === 1 ? "" : "s"} found${data.cached ? " · cached locally" : ""}.`
      : (data.error || "No stations matched. Try broader filters.");
  } catch (error) {
    $("station-status").textContent = `Search failed: ${error.message}`;
    toast(error.message, true);
  } finally {
    stationLoading = false;
  }
}

async function toggleStationFavourite(station) {
  const saved = stationSaved(station);
  const path = saved ? "/api/radio/favourites/remove" : "/api/radio/favourites/add";
  try {
    const data = await postJSON(path, saved ? { uuid: station.uuid } : { station });
    stationFavourites = data.items || [];
    renderStationFavourites();
    renderStationGrid();
    toast(saved ? "Station removed from saved" : "Station saved locally");
  } catch (error) {
    toast(error.message, true);
  }
}

$("station-search-btn").addEventListener("click", () => runStationSearch(false));
$("station-refresh").addEventListener("click", () => runStationSearch(true));
$("station-query").addEventListener("keydown", (event) => {
  if (event.key === "Enter") runStationSearch(false);
});
$("radio-play").addEventListener("click", () => {
  const name = $("radio-name").value.trim() || "Internet Radio";
  const url = $("radio-url").value.trim();
  if (!url) return toast("Enter a stream URL first", true);
  setPendingNow(name, "Tuning in…", "Internet Radio");
  act("radio_url", { name, url }, "Starting stream");
});
$("radio-add").addEventListener("click", () => {
  const name = $("radio-name").value.trim() || "Internet Radio";
  const url = $("radio-url").value.trim();
  if (!url) return toast("Enter a stream URL first", true);
  addToPlaylist({ key: `u:${url}`, kind: "url", title: name, artist: "Stream URL", album: "", source: "Radio URL", url });
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

// The local controller exits shortly after the last browser window closes.
// Every tab (including the mini player) has its own heartbeat, so another open
// window keeps the controller alive when one of them is closed.
const clientId = (() => {
  try { return crypto.randomUUID(); } catch (_) { /* not a secure context */ }
  return `tab-${Date.now()}-${Math.random().toString(36).slice(2)}`;
})();
const keepaliveUrl = `/api/keepalive?client=${encodeURIComponent(clientId)}`;
const shutdownUrl = `/api/shutdown?client=${encodeURIComponent(clientId)}`;
function heartbeat() { api(keepaliveUrl).catch(() => {}); }
heartbeat();
setInterval(heartbeat, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) heartbeat(); });
window.addEventListener("pagehide", (event) => {
  if (event.persisted) return;
  if (navigator.sendBeacon) navigator.sendBeacon(shutdownUrl, "");
  else fetch(shutdownUrl, { method: "POST", keepalive: true }).catch(() => {});
});

setLibraryView(libraryView);
setAdvancedSettings(advancedSettings);
loadMusicDbSettings();
if ($("setting-library-view")) {
  $("setting-library-view").value = libraryView;
  $("setting-library-view").addEventListener("change", (event) => setLibraryView(event.target.value));
}
showView(location.hash.replace(/^#/, "") || "now");
renderPlaylist();
loadServerPlaylists();
loadFavourites();
connectEvents();
loadConfig();
refresh();
setMini(new URLSearchParams(location.search).has("mini"));
