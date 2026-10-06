"use strict";
/* ChordForge web client. All DOM is built with textContent/createElement (no innerHTML with data). */
const $ = (id) => document.getElementById(id);
const state = { config: null, user: null, projects: [], current: null, authMode: "register",
  pollTimer: null, mixStarted: 0, busy: false,
  clips: [], structure: { sections: [] }, clipRec: null };

// ---------- helpers ----------
function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
  return n;
}
class ApiError extends Error { constructor(m, code, status) { super(m); this.code = code; this.status = status; } }
async function api(method, path, body, raw, extraHeaders) {
  const opts = { method, credentials: "same-origin", headers: { "X-Requested-With": "chordforge" } };
  if (raw) { opts.body = raw; opts.headers["Content-Type"] = "application/octet-stream"; }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  if (extraHeaders) Object.assign(opts.headers, extraHeaders);
  let res;
  try { res = await fetch(path, opts); } catch { throw new ApiError("Can't reach the server. Check your connection.", "network", 0); }
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new ApiError((data && data.error && data.error.message) || "Something went wrong.", data && data.error && data.error.code, res.status);
  return data;
}
let toastTimer;
function toast(msg) { const t = $("toast"); t.textContent = msg; t.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3500); }
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

// ---------- auth ----------
function setAuthMode(mode) {
  state.authMode = mode;
  const reg = mode === "register";
  $("tab-register").classList.toggle("active", reg); $("tab-login").classList.toggle("active", !reg);
  $("tab-register").setAttribute("aria-selected", String(reg)); $("tab-login").setAttribute("aria-selected", String(!reg));
  $("auth-submit").textContent = reg ? "Create account" : "Sign in";
  $("auth-password").autocomplete = reg ? "new-password" : "current-password";
  $("auth-hint").hidden = !reg; $("auth-error").hidden = true;
}
async function submitAuth(e) {
  e.preventDefault();
  const err = $("auth-error"); err.hidden = true;
  const email = $("auth-email").value.trim(), password = $("auth-password").value;
  if (!email || !password) { err.textContent = "Enter your email and password."; err.hidden = false; return; }
  $("auth-submit").disabled = true;
  try {
    const r = await api("POST", state.authMode === "register" ? "/api/auth/register" : "/api/auth/login", { email, password });
    $("auth-password").value = "";
    await enterStudio(r.user);
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  finally { $("auth-submit").disabled = false; }
}
async function logout() {
  stopPolling(); try { await api("POST", "/api/auth/logout"); } catch { /* ignore */ }
  state.user = null; state.current = null; state.projects = [];
  $("view-studio").hidden = true; $("view-landing").hidden = false; $("userbar").hidden = true;
}
async function enterStudio(user) {
  state.user = user;
  $("user-email").textContent = user.email; $("userbar").hidden = false;
  $("view-landing").hidden = true; $("view-studio").hidden = false;
  await refreshProjects();
  if (state.projects.length) await openProject(state.projects[0].id); else showNoProject();
}

// ---------- projects ----------
async function refreshProjects() {
  const r = await api("GET", "/api/projects"); state.projects = r.projects; renderProjectList();
}
function renderProjectList() {
  const ul = $("project-list"); ul.replaceChildren();
  $("project-empty").hidden = state.projects.length > 0;
  for (const p of state.projects) {
    const status = p.mixdown.status === "succeeded" && p.mixdown.has_audio ? "built"
      : ["queued", "running"].includes(p.mixdown.status) ? "building…" : "in progress";
    ul.append(el("li", { class: "project-item" + (state.current && state.current.id === p.id ? " active" : "") },
      el("button", { class: "project-open", type: "button", onclick: () => openProject(p.id), "aria-label": "Open " + p.name },
        el("span", { class: "pname", text: p.name }), el("span", { class: "pmeta", text: status })),
      el("button", { class: "project-del", type: "button", "aria-label": "Delete " + p.name, title: "Delete", onclick: () => deleteProject(p), text: "×" })));
  }
}
function showNoProject() { state.current = null; $("no-project").hidden = false; $("project-view").hidden = true; renderProjectList(); }
async function newProject() {
  try { const r = await api("POST", "/api/projects", {}); await refreshProjects(); await openProject(r.project.id); }
  catch (ex) { toast(ex.message); }
}
async function deleteProject(p) {
  if (!confirm(`Delete "${p.name}" and its audio? This can't be undone.`)) return;
  try {
    await api("DELETE", "/api/projects/" + p.id);
    if (state.current && state.current.id === p.id) { stopPolling(); state.current = null; }
    await refreshProjects();
    if (state.current) renderProject(); else if (state.projects.length) await openProject(state.projects[0].id); else showNoProject();
  } catch (ex) { toast(ex.message); }
}
async function openProject(id) {
  stopPolling();
  try { const r = await api("GET", "/api/projects/" + id); setCurrent(r.project); }
  catch (ex) { toast(ex.message); }
}
function setCurrent(p) {
  const projectChanged = !state.current || state.current.id !== p.id;
  state.current = p; renderProject(); renderProjectList();
  if (["queued", "running"].includes(p.mixdown.status)) startPolling();
  if (projectChanged) loadClipsAndStructure(p.id); else { renderClips(); renderSections(); }
}

// ---------- rendering ----------
function fillSelect(sel, items, value) {
  sel.replaceChildren(...items.map((i) => el("option", { value: i.id, text: i.label })));
  if (value) sel.value = value;
}
function renderProject() {
  const p = state.current;
  $("no-project").hidden = true; $("project-view").hidden = false;
  if (document.activeElement !== $("project-name")) $("project-name").value = p.name;
  renderMixdown(p);
}
function renderMixdown(p) {
  const g = p.mixdown, busy = ["queued", "running"].includes(g.status);
  $("mix-progress").hidden = !busy; $("mix-failed").hidden = g.status !== "failed";
  $("mix-done").hidden = !(g.status === "succeeded" && g.has_audio);
  $("btn-mixdown").disabled = busy || state.busy;
  $("btn-mixdown").textContent = busy ? "BUILDING…" : g.status === "failed" ? "TRY AGAIN" : g.has_audio ? "BUILD AGAIN" : "BUILD MY SONG";
  if (busy) updateProgressText(g.status);
  if (g.status === "failed") $("mix-failed").textContent = g.error || "Building your song failed. Please try again.";
  if (g.status === "succeeded" && g.has_audio) {
    const pl = $("player-mixdown"), src = `/api/projects/${p.id}/mixdown?v=${encodeURIComponent(g.updated_at || "")}`;
    if (pl.dataset.src !== src) { pl.src = src; pl.dataset.src = src; }
    $("btn-download").href = `/api/projects/${p.id}/mixdown?download=1`;
    $("mix-meta").textContent = g.duration ? `${fmtTime(g.duration)} · WAV` : "WAV";
  }
}
function updateProgressText(status) {
  if (!state.mixStarted) state.mixStarted = Date.now();
  const secs = Math.round((Date.now() - state.mixStarted) / 1000);
  $("mix-progress-text").textContent = (status === "queued" ? "Queued…" : "Building your song…") + ` ${secs}s`;
}

// ---------- audio capture / upload ----------
function encodeWav(channelData, sampleRate) {
  const n = channelData.length, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) { const s = Math.max(-1, Math.min(1, channelData[i])); v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
  return new Blob([buf], { type: "audio/wav" });
}
async function toWav(blob) {
  const Ctx = window.AudioContext || window.webkitAudioContext; const ctx = new Ctx();
  try {
    const ab = await ctx.decodeAudioData(await blob.arrayBuffer());
    const len = ab.length, mono = new Float32Array(len);
    for (let c = 0; c < ab.numberOfChannels; c++) { const d = ab.getChannelData(c); for (let i = 0; i < len; i++) mono[i] += d[i] / ab.numberOfChannels; }
    return encodeWav(mono, ab.sampleRate);
  } finally { ctx.close(); }
}

// ---------- mixdown ("Build my song") ----------
async function startMixdown() {
  const err = $("mix-error"); err.hidden = true;
  try {
    const r = await api("POST", `/api/projects/${state.current.id}/mixdown`);
    state.mixStarted = Date.now(); setCurrent(r.project); startPolling();
    $("card-mix").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (ex) { err.textContent = ex.message; err.hidden = false; }
}
function startPolling() {
  stopPolling(); if (!state.mixStarted) state.mixStarted = Date.now();
  state.pollTimer = setInterval(async () => {
    if (!state.current) return stopPolling();
    updateProgressText(state.current.mixdown.status);
    try {
      const r = await api("GET", "/api/projects/" + state.current.id);
      if (!state.current || state.current.id !== r.project.id) return;
      state.current = r.project; renderMixdown(r.project);
      if (!["queued", "running"].includes(r.project.mixdown.status)) { stopPolling(); state.mixStarted = 0; await refreshProjects(); }
    } catch (ex) { if (ex.status === 401) { stopPolling(); await logout(); } }
  }, 1500);
}
function stopPolling() { if (state.pollTimer) clearInterval(state.pollTimer); state.pollTimer = null; }

// ---------- actions ----------
async function saveProject() {
  try {
    const r = await api("PATCH", "/api/projects/" + state.current.id, { name: $("project-name").value });
    state.current = r.project; await refreshProjects(); toast("Song saved.");
  } catch (ex) { toast(ex.message); }
}

// ---------- parts / song structure ----------
async function loadClipsAndStructure(pid) {
  try {
    const [cr, sr] = await Promise.all([api("GET", `/api/projects/${pid}/clips`), api("GET", `/api/projects/${pid}/structure`)]);
    if (!state.current || state.current.id !== pid) return;  // user navigated away meanwhile
    state.clips = cr.clips; state.structure = sr.structure;
  } catch { state.clips = []; state.structure = { sections: [] }; }
  renderClips(); renderSections();
}
function renderClips() {
  if (!$("clip-track-type").options.length) fillSelect($("clip-track-type"), state.config.track_types);
  const box = $("clip-list"); box.replaceChildren();
  $("clip-empty").hidden = state.clips.length > 0;
  const refSel = $("clip-ref-select"), prevRef = refSel.value;
  refSel.replaceChildren(el("option", { value: "", text: "No reference (record alone)" }),
    ...state.clips.map((c) => el("option", { value: c.id, text: c.label })));
  refSel.value = state.clips.some((c) => c.id === prevRef) ? prevRef : "";
  for (const c of state.clips) {
    const label = el("input", { class: "clip-label", type: "text", value: c.label, maxlength: "60",
      "aria-label": "Part name", onchange: (e) => patchClip(c.id, { label: e.target.value }) });
    const typeLabel = (state.config.track_types.find((t) => t.id === c.track_type) || {}).label || c.track_type;
    const trimStart = el("input", { type: "number", min: "0", step: "0.1", value: c.trim_start.toFixed(1) });
    const trimEnd = el("input", { type: "number", min: "0", step: "0.1", value: c.trim_end.toFixed(1) });
    const applyTrim = () => {
      const ts = Number(trimStart.value), te = Number(trimEnd.value);
      if (isFinite(ts) && isFinite(te) && ts < te) patchClip(c.id, { trim_start: ts, trim_end: te });
      else { trimStart.value = c.trim_start.toFixed(1); trimEnd.value = c.trim_end.toFixed(1); }
    };
    trimStart.addEventListener("change", applyTrim); trimEnd.addEventListener("change", applyTrim);
    const gain = el("input", { type: "range", min: "0.1", max: "3", step: "0.1", value: String(c.gain),
      "aria-label": "Volume", onchange: (e) => patchClip(c.id, { gain: Number(e.target.value) }) });
    const mute = el("input", { type: "checkbox", checked: c.muted ? "" : false,
      onchange: (e) => patchClip(c.id, { muted: e.target.checked }) });
    const del = el("button", { class: "btn btn-sm", type: "button", onclick: () => deleteClip(c.id) }, "Delete");
    const audio = el("audio", { controls: "", preload: "metadata", src: `/api/projects/${state.current.id}/clips/${c.id}/audio` });
    box.append(el("div", { class: "clip-row" },
      el("div", { class: "clip-head" }, label, el("span", { class: "clip-type-badge", text: typeLabel }), del),
      el("div", { class: "clip-controls" },
        el("span", { class: "trim-field" }, "Trim", trimStart, "–", trimEnd, "s"),
        el("span", { class: "gain-field" }, "Volume", gain),
        el("label", { class: "check" }, mute, "Mute")),
      audio));
  }
}
function renderSections() {
  const box = $("section-list"); box.replaceChildren();
  const sections = state.structure.sections;
  sections.forEach((sec, i) => {
    const name = el("input", { class: "section-name", type: "text", value: sec.name, maxlength: "30",
      "aria-label": "Section name", onchange: (e) => { sec.name = e.target.value || sec.name; saveStructure(); } });
    const up = el("button", { class: "sec-btn", type: "button", title: "Move up", "aria-label": "Move section up",
      disabled: i === 0 ? "" : false, onclick: () => { [sections[i - 1], sections[i]] = [sections[i], sections[i - 1]]; saveStructure(); } }, "↑");
    const down = el("button", { class: "sec-btn", type: "button", title: "Move down", "aria-label": "Move section down",
      disabled: i === sections.length - 1 ? "" : false, onclick: () => { [sections[i + 1], sections[i]] = [sections[i], sections[i + 1]]; saveStructure(); } }, "↓");
    const del = el("button", { class: "sec-btn", type: "button", title: "Delete section", "aria-label": "Delete section",
      onclick: () => { sections.splice(i, 1); saveStructure(); } }, "×");
    const chips = el("div", { class: "section-clips" });
    for (const c of state.clips) {
      const assignment = sec.clips.find((x) => x.clip_id === c.id);
      const inc = el("input", { type: "checkbox", checked: assignment ? "" : false,
        onchange: (e) => {
          if (e.target.checked) sec.clips.push({ clip_id: c.id, loop: true });
          else sec.clips = sec.clips.filter((x) => x.clip_id !== c.id);
          saveStructure();
        } });
      const loop = el("input", { type: "checkbox", checked: (assignment ? assignment.loop : true) ? "" : false,
        onchange: (e) => { const a = sec.clips.find((x) => x.clip_id === c.id); if (a) { a.loop = e.target.checked; saveStructure(); } } });
      chips.append(el("label", { class: "section-clip-chip" }, inc, c.label,
        el("span", { class: "loop-toggle" }, loop, "loop")));
    }
    box.append(el("div", { class: "section-row" },
      el("div", { class: "section-head" }, name, up, down, del),
      chips));
  });
  $("structure-hint").textContent = state.clips.length === 0
    ? "Record or upload a part above to start building sections."
    : sections.length === 0 ? "Add a section to start arranging your parts into a song."
    : "Building your song uses this structure once at least one section has a part assigned.";
}
let structureSaveSeq = 0;
async function saveStructure() {
  const mySeq = ++structureSaveSeq;
  const pid = state.current.id;
  try {
    const r = await api("PUT", `/api/projects/${pid}/structure`, state.structure);
    // If another save started after this one (rapid clicks), its response -- not this stale
    // one -- should win; applying an older response here would silently roll back a newer edit.
    if (mySeq !== structureSaveSeq || !state.current || state.current.id !== pid) return;
    state.structure = r.structure; renderSections();
  } catch (ex) {
    if (mySeq !== structureSaveSeq) return;
    toast(ex.message); loadClipsAndStructure(pid);
  }
}
function addSection() {
  state.structure.sections.push({ name: `Section ${state.structure.sections.length + 1}`, clips: [] });
  saveStructure();
}
async function patchClip(id, patch) {
  try {
    const r = await api("PATCH", `/api/projects/${state.current.id}/clips/${id}`, patch);
    const i = state.clips.findIndex((c) => c.id === id);
    if (i >= 0) state.clips[i] = r.clip;
  } catch (ex) { toast(ex.message); renderClips(); }
}
async function deleteClip(id) {
  try {
    await api("DELETE", `/api/projects/${state.current.id}/clips/${id}`);
    state.clips = state.clips.filter((c) => c.id !== id);
    for (const sec of state.structure.sections) sec.clips = sec.clips.filter((x) => x.clip_id !== id);
    renderClips(); renderSections();
  } catch (ex) { toast(ex.message); }
}
async function uploadClipBlob(blob, trackType, label) {
  try {
    const r = await api("PUT", `/api/projects/${state.current.id}/clips`, undefined, blob, {
      "X-Track-Type": trackType, "X-Label": encodeURIComponent(label || "") });
    state.clips.push(r.clip); renderClips(); renderSections();
    $("clip-status").textContent = "Part added."; $("clip-status").className = "status ok";
  } catch (ex) { $("clip-status").textContent = ex.message; $("clip-status").className = "status err"; }
}
async function onClipFileChosen(e) {
  const f = e.target.files[0]; e.target.value = ""; if (!f || !state.current) return;
  const limit = state.config.limits.max_upload_mb * 1024 * 1024;
  if (f.size > limit) { $("clip-status").textContent = `That file is too large (limit ${state.config.limits.max_upload_mb} MB).`; $("clip-status").className = "status err"; return; }
  let blob = f;
  if (!/\.wav$/i.test(f.name)) { try { blob = await toWav(f); } catch { blob = f; } }
  $("clip-status").textContent = "Uploading…"; $("clip-status").className = "status";
  await uploadClipBlob(blob, $("clip-track-type").value, $("clip-label").value);
  $("clip-label").value = "";
}
function stopReferencePlayback() {
  const ref = $("ref-audio");
  try { ref.pause(); ref.currentTime = 0; } catch { /* ignore */ }
}
async function startRecordingClip() {
  if (!navigator.mediaDevices || !window.MediaRecorder) { $("clip-status").textContent = "Recording isn't supported in this browser. Use Upload instead."; $("clip-status").className = "status err"; return; }
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } }); }
  catch { $("clip-status").textContent = "Microphone access was blocked."; $("clip-status").className = "status err"; return; }
  const mr = new MediaRecorder(stream), chunks = [];
  const Ctx = window.AudioContext || window.webkitAudioContext, actx = new Ctx(), an = actx.createAnalyser();
  an.fftSize = 1024; actx.createMediaStreamSource(stream).connect(an);
  const data = new Uint8Array(an.fftSize), started = Date.now(), max = state.config.limits.max_duration_s;
  const tick = setInterval(() => {
    an.getByteTimeDomainData(data); let peak = 0; for (const x of data) peak = Math.max(peak, Math.abs(x - 128));
    $("meter-bar").style.width = Math.min(100, (peak / 128) * 140) + "%";
    const s = (Date.now() - started) / 1000; $("rec-time").textContent = fmtTime(s);
    if (s >= max) stopRecordingClip();
  }, 80);
  mr.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  mr.onstop = async () => {
    clearInterval(tick); stream.getTracks().forEach((t) => t.stop()); actx.close();
    stopReferencePlayback();
    $("rec-live").hidden = true; $("btn-stop-clip").hidden = true; $("btn-record-clip").hidden = false;
    state.clipRec = null;
    $("clip-status").textContent = "Processing…"; $("clip-status").className = "status";
    try { await uploadClipBlob(await toWav(new Blob(chunks, { type: mr.mimeType })), $("clip-track-type").value, $("clip-label").value); $("clip-label").value = ""; }
    catch { $("clip-status").textContent = "That recording couldn't be processed."; $("clip-status").className = "status err"; }
  };
  // Reference playback: if the user picked an existing part to hear while recording, start it
  // as close as possible to the moment recording starts, from that part's trim-in point (so what
  // they hear roughly matches what would actually play in the mix). Browser/OS audio latency
  // means this can't be sample-accurate -- that's disclosed in the UI, not hidden.
  const refId = $("clip-ref-select").value;
  const refClip = refId ? state.clips.find((c) => c.id === refId) : null;
  if (refClip) {
    const ref = $("ref-audio");
    ref.src = `/api/projects/${state.current.id}/clips/${refClip.id}/audio`;
    ref.currentTime = 0;
    try { await ref.play(); ref.currentTime = refClip.trim_start || 0; } catch { /* autoplay blocked or unsupported; recording still proceeds */ }
  }
  state.clipRec = mr; mr.start();
  $("rec-live").hidden = false; $("btn-record-clip").hidden = true; $("btn-stop-clip").hidden = false;
  $("clip-status").textContent = refClip ? `Recording (playing back "${refClip.label}")…` : "Recording…";
  $("clip-status").className = "status";
}
function stopRecordingClip() { if (state.clipRec && state.clipRec.state !== "inactive") state.clipRec.stop(); }

// ---------- init ----------
async function init() {
  $("tab-register").addEventListener("click", () => setAuthMode("register"));
  $("tab-login").addEventListener("click", () => setAuthMode("login"));
  $("auth-form").addEventListener("submit", submitAuth);
  $("btn-logout").addEventListener("click", logout);
  $("btn-new").addEventListener("click", newProject); $("btn-start").addEventListener("click", newProject);
  $("btn-record-clip").addEventListener("click", startRecordingClip);
  $("btn-stop-clip").addEventListener("click", stopRecordingClip);
  $("clip-file-input").addEventListener("change", onClipFileChosen);
  $("btn-add-section").addEventListener("click", addSection);
  $("btn-save").addEventListener("click", saveProject);
  $("btn-mixdown").addEventListener("click", startMixdown);
  try { state.config = await api("GET", "/api/config"); } catch { toast("Can't reach the server."); return; }
  try { const me = await api("GET", "/api/auth/me"); await enterStudio(me.user); }
  catch { $("view-landing").hidden = false; }
}
init();
